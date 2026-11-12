// SPDX-License-Identifier: GPL-2.0-only
/* Fixed, model-validated battery commands.
 * No arbitrary EC access. Userspace must renew a 15-second kernel lease.
 * mode: 0 automatic, 1 AC without charging, 2 battery while AC is connected. */
#include <linux/acpi.h>
#include <linux/dmi.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/mutex.h>
#include <linux/workqueue.h>
#include <linux/suspend.h>
#include <linux/reboot.h>
#include <linux/power_supply.h>
#include <linux/delay.h>

static struct platform_device *pou_battery;
static DEFINE_MUTEX(battery_lock);
static bool owned, blocked, unloading;
static unsigned int current_mode;
static unsigned long deadline;
static const char *reason = "automatic";
/* Arm only immediately before an observed transition. A stale userspace
 * preference cannot keep an awake battery inhibited after a failed suspend. */
static unsigned int armed_transition;
static unsigned long arm_deadline;
static bool offline_held;
static unsigned int offline_caps; /* bit 0: deep suspend, bit 1: complete power-off */
static const char *ec_command;

/* Called with battery_lock held. The only caller-controlled values are our
 * fixed command constants. EC88 delegates to serialized EC87/EC86, sharing
 * the firmware's command serialization with fan and platform methods. */
static int battery_command(u8 subcommand, u8 arg0, u8 arg1, u8 *result_byte)
{
    const u8 values[4] = {0x8b, subcommand, arg0, arg1};
    union acpi_object args[4] = {0};
    struct acpi_object_list list = {.count = 4, .pointer = args};
    unsigned long long result;
    int i;
    for (i = 0; i < 4; i++) { args[i].type = ACPI_TYPE_INTEGER; args[i].integer.value = values[i]; }
    if (ACPI_FAILURE(acpi_evaluate_integer(NULL, (char *)ec_command, &list, &result)))
        return -EIO;
    if (result > U32_MAX || (result & 0xff)) return -EIO;
    *result_byte = (result >> 8) & 0xff;
    return 0;
}

static int ac_state(u8 *state) { return battery_command(0x16, 0xfe, 0xfe, state); }
static int restore_locked(void)
{
    u8 result;
    int ac_error, charge_error;
    if (!owned) return 0;
    /* Try both restoration commands even if the first one fails. */
    ac_error = battery_command(0x16, 0xff, 0xff, &result);
    if (!ac_error) ac_error = ac_state(&result);
    if (!ac_error && result) ac_error = -EIO;
    charge_error = battery_command(7, 3, 0, &result);
    if (!charge_error && result != 3) charge_error = -EIO;
    if (!ac_error && !charge_error) { owned = false; current_mode = 0; offline_held = false; }
    return ac_error ? ac_error : charge_error;
}

static int restore_retry_locked(void)
{
    int retry, error;
    for (retry = 0; retry < 3; retry++) {
        error = restore_locked();
        if (!error) return 0;
        msleep(20);
    }
    return error;
}

static int supply_value(const char *name, enum power_supply_property prop)
{
    struct power_supply *supply = power_supply_get_by_name(name);
    union power_supply_propval value;
    int error;
    if (!supply) return -ENODEV;
    error = power_supply_get_property(supply, prop, &value);
    power_supply_put(supply);
    return error ? error : value.intval;
}

static void watch(struct work_struct *work);
/* This SKU has one UCSI sink. AC can report offline on a live PD contract. */
static bool external_supply_online(void)
{
    const char *usb = "ucsi-source-psy-USBC000:001";
    int online;

    if (supply_value("AC", POWER_SUPPLY_PROP_ONLINE) == 1)
        return true;
    if (supply_value(usb, POWER_SUPPLY_PROP_SCOPE) != POWER_SUPPLY_SCOPE_SYSTEM)
        return false;
    online = supply_value(usb, POWER_SUPPLY_PROP_ONLINE);
    return online == 1 || online == 2;
}

static DECLARE_DELAYED_WORK(watchdog, watch);
static void watch(struct work_struct *work)
{
    int capacity, error = 0;
    bool release = false;
    mutex_lock(&battery_lock);
    if (owned) {
        if (time_after_eq(jiffies, deadline)) { reason = "expired"; release = true; }
        if (supply_value("BAT1", POWER_SUPPLY_PROP_PRESENT) != 1) { reason = "battery-missing"; release = true; }
        if (current_mode == 2) {
            capacity = supply_value("BAT1", POWER_SUPPLY_PROP_CAPACITY);
            if (capacity < 40 || capacity > 100) { reason = "capacity-guard"; release = true; }
        }
        if (release || blocked || unloading) error = restore_locked();
        if (error) pr_err_ratelimited("pou_battery: automatic restoration failed %d; retrying\n", error);
        if (owned && !unloading) schedule_delayed_work(&watchdog, HZ);
    }
    mutex_unlock(&battery_lock);
}

static ssize_t mode_show(struct device *dev, struct device_attribute *a, char *buf)
{
    unsigned int mode;
    mutex_lock(&battery_lock); mode = current_mode; mutex_unlock(&battery_lock);
    return sysfs_emit(buf, "%u\n", mode);
}
static ssize_t mode_store(struct device *dev, struct device_attribute *a, const char *buf, size_t count)
{
    unsigned int mode;
    int error = 0, capacity;
    u8 actual, response;
    if (kstrtouint(buf, 10, &mode) || mode > 2) return -EINVAL;
    mutex_lock(&battery_lock);
    if (!mode) { reason = "automatic"; error = restore_locked(); goto done; }
    if (blocked || unloading) { error = -EBUSY; goto done; }
    capacity = supply_value("BAT1", POWER_SUPPLY_PROP_CAPACITY);
    if (supply_value("BAT1", POWER_SUPPLY_PROP_PRESENT) != 1 || capacity < 0 || capacity > 100 ||
        (mode == 2 && capacity < 40)) { error = -ENODATA; goto done; }
    error = ac_state(&actual);
    if (error) goto done;
    if (actual != 0 && !(owned && current_mode == 2 && actual == 0x41)) {
        error = -EBUSY; goto done;
    }
    if (!owned && !external_supply_online()) { error = -ENODEV; goto done; }
    deadline = jiffies + 15 * HZ;
    if (!owned || mode != current_mode || (mode == 2 && actual != 0x41)) {
        /* Arm recovery before touching hardware, including partial errors. */
        owned = true;
        if (mode == 1) {
            error = battery_command(7, 1, 0, &response);
            if (!error && response != 1) error = -EIO;
            if (!error) error = battery_command(0x16, 0xff, 0xff, &response);
            if (!error) error = ac_state(&actual);
            if (!error && actual) error = -EIO;
        } else {
            error = battery_command(0x16, 100, 0, &response);
            if (!error && response != 0x41) error = -EIO;
            if (!error) error = ac_state(&actual);
            if (!error && actual != 0x41) error = -EIO;
        }
        if (!error) current_mode = mode;
    }
    if (error) { reason = "command-error"; restore_locked(); }
    else reason = mode == 1 ? "holding" : "discharging";
done:
    if (error && owned) deadline = jiffies;
    if (owned) mod_delayed_work(system_wq, &watchdog, HZ);
    mutex_unlock(&battery_lock);
    return error ? error : count;
}
static DEVICE_ATTR_RW(mode);
static ssize_t ac_override_show(struct device *dev, struct device_attribute *a, char *buf)
{
    u8 actual;
    int error;
    mutex_lock(&battery_lock); error = ac_state(&actual); mutex_unlock(&battery_lock);
    return error ? error : sysfs_emit(buf, "%u\n", actual);
}
static DEVICE_ATTR_RO(ac_override);
static ssize_t lease_seconds_show(struct device *dev, struct device_attribute *a, char *buf)
{
    unsigned int seconds = 0;
    mutex_lock(&battery_lock);
    if (owned && time_before(jiffies, deadline)) seconds = DIV_ROUND_UP(deadline-jiffies, HZ);
    mutex_unlock(&battery_lock);
    return sysfs_emit(buf, "%u\n", seconds);
}
static DEVICE_ATTR_RO(lease_seconds);
static ssize_t reason_show(struct device *dev, struct device_attribute *a, char *buf)
{
    ssize_t result;
    mutex_lock(&battery_lock); result = sysfs_emit(buf, "%s\n", reason); mutex_unlock(&battery_lock);
    return result;
}
static DEVICE_ATTR_RO(reason);
static ssize_t interface_version_show(struct device *dev, struct device_attribute *a, char *buf)
{
    return sysfs_emit(buf, "1\n");
}
static DEVICE_ATTR_RO(interface_version);
static ssize_t offline_capabilities_show(struct device *dev, struct device_attribute *a, char *buf)
{
    return sysfs_emit(buf, "%u\n", offline_caps);
}
static DEVICE_ATTR_RO(offline_capabilities);
static ssize_t arm_transition_store(struct device *dev, struct device_attribute *a,
                                    const char *buf, size_t count)
{
    unsigned int value;
    int err = kstrtouint(buf, 10, &value);
    if (err || value > 2 || (value && !(value & offline_caps)))
        return -EINVAL;
    mutex_lock(&battery_lock);
    if (unloading || blocked) {
        mutex_unlock(&battery_lock);
        return -EBUSY;
    }
    armed_transition = value;
    arm_deadline = jiffies + 120 * HZ;
    mutex_unlock(&battery_lock);
    return count;
}
static DEVICE_ATTR_WO(arm_transition);
static struct attribute *attrs[] = {&dev_attr_mode.attr, &dev_attr_ac_override.attr,
    &dev_attr_offline_capabilities.attr, &dev_attr_arm_transition.attr,
    &dev_attr_lease_seconds.attr, &dev_attr_reason.attr, &dev_attr_interface_version.attr, NULL};
static const struct attribute_group group = {.attrs = attrs};

static int transition_begin(unsigned int transition)
{
    bool armed = transition && armed_transition == transition && time_before(jiffies, arm_deadline);
    u8 result;
    int err = restore_retry_locked();
    armed_transition = 0;
    if (err || !armed || supply_value("AC", POWER_SUPPLY_PROP_ONLINE) != 1 ||
        supply_value("BAT1", POWER_SUPPLY_PROP_PRESENT) != 1)
        return err;
    offline_held = true;
    err = battery_command(7, 1, 0, &result);
    if (!err && result != 1)
        err = -EIO;
    if (err) {
        owned = true;
        if (!restore_retry_locked())
            offline_held = false;
    } else {
        pr_info("pou_battery: charging paused for transition %u\n", transition);
    }
    return err;
}

static int transition_end(void)
{
    int err;
    if (!offline_held)
        return 0;
    owned = true;
    err = restore_retry_locked();
    if (!err)
        offline_held = false;
    return err;
}

static int pm_event(struct notifier_block *nb, unsigned long event, void *unused)
{
    int error = 0;
    mutex_lock(&battery_lock);
    if (event == PM_SUSPEND_PREPARE || event == PM_HIBERNATION_PREPARE || event == PM_RESTORE_PREPARE) {
        blocked = true; reason = "sleep";
        error = transition_begin(event == PM_SUSPEND_PREPARE ? 1 : 0);
        if (error) blocked = false;
    } else if (event == PM_POST_SUSPEND || event == PM_POST_HIBERNATION || event == PM_POST_RESTORE) {
        error = transition_end();
        blocked = false;
    }
    if (error && owned) mod_delayed_work(system_wq, &watchdog, HZ);
    mutex_unlock(&battery_lock);
    return error ? NOTIFY_BAD : NOTIFY_OK;
}
static int reboot_event(struct notifier_block *nb, unsigned long event, void *unused)
{
    mutex_lock(&battery_lock);
    blocked = true; reason = "shutdown";
    if (transition_begin(event == SYS_POWER_OFF ? 2 : 0))
        pr_err("pou_battery: shutdown charge policy failed\n");
    mutex_unlock(&battery_lock);
    return NOTIFY_DONE;
}
static struct notifier_block pm_nb = {.notifier_call = pm_event};
static struct notifier_block reboot_nb = {.notifier_call = reboot_event};
static int __init battery_init(void)
{
    int error;
    u8 actual;
    if (dmi_match(DMI_PRODUCT_NAME,"CFSV7-3L") && dmi_match(DMI_BIOS_VERSION,"V3.11L22") &&
        dmi_match(DMI_PRODUCT_SKU,"CF-SV7HD9VS")) {
        ec_command = "\\_SB.PCI0.LPCB.EC0.EC88";
        offline_caps = 3;
    } else if (dmi_match(DMI_PRODUCT_NAME,"CFSR3-1") && dmi_match(DMI_BIOS_VERSION,"V1.00L12") &&
               dmi_match(DMI_PRODUCT_SKU,"CF-SR3SKAKS")) {
        ec_command = "\\_SB.PC00.LPCB.EC0.EC88";
        offline_caps = 0;
    } else return -ENODEV;
    mutex_lock(&battery_lock);
    error = ac_state(&actual);
    if (!error && actual != 0 && actual != 0x41) error = -EOPNOTSUPP;
    /* AC override=0 does not prove charging is enabled. Establish an explicit
     * automatic baseline, including a charge-inhibit left by an earlier crash. */
    if (!error) { owned = true; error = restore_retry_locked(); }
    mutex_unlock(&battery_lock);
    if (error) return error;
    pou_battery = platform_device_register_simple("pou-battery", -1, NULL, 0);
    if (IS_ERR(pou_battery)) return PTR_ERR(pou_battery);
    error = register_pm_notifier(&pm_nb);
    if (error) goto remove_device;
    error = register_reboot_notifier(&reboot_nb);
    if (error) goto remove_pm;
    error = sysfs_create_group(&pou_battery->dev.kobj, &group);
    if (!error) return 0;
    unregister_reboot_notifier(&reboot_nb);
remove_pm:
    unregister_pm_notifier(&pm_nb);
remove_device:
    platform_device_unregister(pou_battery);
    return error;
}
static void __exit battery_exit(void)
{
    int error;
    sysfs_remove_group(&pou_battery->dev.kobj, &group);
    unregister_pm_notifier(&pm_nb);
    unregister_reboot_notifier(&reboot_nb);
    mutex_lock(&battery_lock); unloading = true; mutex_unlock(&battery_lock);
    cancel_delayed_work_sync(&watchdog);
    mutex_lock(&battery_lock);
    if (offline_held) owned = true;
    error = restore_retry_locked();
    if (error) pr_err("pou_battery: could not restore automatic charging on unload\n");
    mutex_unlock(&battery_lock);
    platform_device_unregister(pou_battery);
}
module_init(battery_init); module_exit(battery_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("AkaseToshiyuki");
MODULE_VERSION("0.8.0");
MODULE_DESCRIPTION("Validated Panasonic battery controls with expiring kernel lease");
