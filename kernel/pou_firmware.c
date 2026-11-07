// SPDX-License-Identifier: GPL-2.0-only
// CF-SV7-3L V3.11L22: methods verified against the machine's DSDT/SSDT1.
// Calls only verified firmware methods; no raw EC register interface.
#include <linux/acpi.h>
#include <linux/dmi.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/mutex.h>
#include <linux/workqueue.h>
#include <linux/suspend.h>
#include <linux/reboot.h>
#include <linux/pci.h>
#include <linux/jiffies.h>
#include <asm/msr.h>
#include <asm/msr-index.h>

#define EC "\\_SB.PCI0.LPCB.EC0."
static struct platform_device *pou_device;
static DEFINE_MUTEX(pou_lock);
static unsigned long long initial_fan;
static bool changed;
static bool limits_leased;
static bool optical_changed;
static bool allow_experimental_ec_limits;
static bool pm_blocked;
static bool unloading;
static unsigned long lease_deadline;
static const char *lease_reason = "none";
static unsigned int lease_cutoff_c = 94;
module_param(allow_experimental_ec_limits, bool, 0444);
MODULE_PARM_DESC(allow_experimental_ec_limits, "Permit experimental EC dynamic limit bypass (default off)");

/* Root can lower this boundary for validation; it can never exceed 94 C. */
static int set_cutoff(const char *value, const struct kernel_param *kp)
{
    unsigned int candidate;
    int error = kstrtouint(value, 10, &candidate);
    if (error || candidate < 30 || candidate > 94)
        return -EINVAL;
    WRITE_ONCE(lease_cutoff_c, candidate);
    return 0;
}
static const struct kernel_param_ops cutoff_ops = { .set = set_cutoff, .get = param_get_uint };
module_param_cb(lease_cutoff_c, &cutoff_ops, &lease_cutoff_c, 0600);
MODULE_PARM_DESC(lease_cutoff_c, "Lease thermal cutoff, 30..94 C (default 94)");

/* Intel package DTS, same temperature interpretation as Linux coretemp.
 * Read only: neither temperature targets nor any MSR are written. */
static int package_temperature(int *temperature)
{
    u64 target, status;
    int tjmax;
    if (rdmsrq_safe(MSR_IA32_TEMPERATURE_TARGET, &target) ||
        rdmsrq_safe(MSR_IA32_PACKAGE_THERM_STATUS, &status))
        return -EIO;
    tjmax = (target >> 16) & 0xff;
    if (!(status & BIT_ULL(31)) || tjmax < 80 || tjmax > 110)
        return -ENODATA;
    *temperature = tjmax - ((status >> 16) & 0xff);
    return *temperature < 0 || *temperature > 110 ? -ENODATA : 0;
}

#include "pou_fan_control.h"

static int optical_set_power(unsigned int value)
{
    acpi_handle handle;
    struct acpi_device *adev;
    struct pci_dev *controller;
    int error;
    unsigned long long capabilities;
    if (ACPI_FAILURE(acpi_evaluate_integer(NULL, "\\SODS", NULL, &capabilities)) ||
        (capabilities & 9) != 9)
        return -EOPNOTSUPP;
    if (ACPI_FAILURE(acpi_get_handle(NULL, "\\_SB.PCI0.SAT0.PRT1", &handle)))
        return -ENODEV;
    adev = acpi_fetch_acpi_dev(handle);
    if (!adev)
        return -ENODEV;
    if (value)
        return acpi_device_set_power(adev, ACPI_STATE_D0);
    controller = pci_get_domain_bus_and_slot(0, 0, PCI_DEVFN(0x17, 0));
    if (!controller)
        return -ENODEV;
    device_lock(&controller->dev);
    /* Do not cut power while ATA/ZPODD resources are still registered. */
    error = controller->driver ? -EBUSY : acpi_device_set_power(adev, ACPI_STATE_D3_COLD);
    device_unlock(&controller->dev);
    pci_dev_put(controller);
    return error;
}

static int set_limits(unsigned int value)
{
    union acpi_object arg = { .type = ACPI_TYPE_INTEGER };
    struct acpi_object_list args = { .count = 1, .pointer = &arg };
    unsigned long long actual;
    arg.integer.value = value;
    if (ACPI_FAILURE(acpi_evaluate_object(NULL, EC "SEPL", &args, NULL)) ||
        ACPI_FAILURE(acpi_evaluate_integer(NULL, EC "EPLE", NULL, &actual)) || actual != value)
        return -EIO;
    return 0;
}

/* All lease state is protected by pou_lock. */
static int restore_limits_locked(void)
{
    if (limits_leased) {
        int error = set_limits(1);
        if (error) {
            pr_warn_ratelimited("pou_firmware: restoring dynamic limits failed\n");
            return error;
        }
        limits_leased = false;
    }
    return 0;
}
static int restore_limits(void)
{
    int error;
    mutex_lock(&pou_lock);
    error = restore_limits_locked();
    mutex_unlock(&pou_lock);
    return error;
}
static void check_limits(struct work_struct *work);
static DECLARE_DELAYED_WORK(limits_watchdog, check_limits);

static void check_limits(struct work_struct *work)
{
    int temp;
    const char *reason = NULL;
    mutex_lock(&pou_lock);
    if (limits_leased) {
        if (pm_blocked || unloading)
            reason = "sleep_or_unload";
        else if (time_after_eq(jiffies, lease_deadline))
            reason = "expired";
        else if (package_temperature(&temp))
            reason = "sensor";
        else if (temp >= READ_ONCE(lease_cutoff_c))
            reason = "temperature";
        if (reason) {
            lease_reason = reason;
            restore_limits_locked();
        }
        if (limits_leased && !pm_blocked && !unloading)
            schedule_delayed_work(&limits_watchdog, msecs_to_jiffies(1000));
    }
    mutex_unlock(&pou_lock);
}

static int pou_pm_event(struct notifier_block *nb, unsigned long action, void *unused)
{
    if (action == PM_SUSPEND_PREPARE || action == PM_HIBERNATION_PREPARE) {
        int error;
        mutex_lock(&pou_lock);
        pm_blocked = true;
        error = fan_release();
        lease_reason = "sleep";
        mutex_unlock(&pou_lock);
        cancel_delayed_work_sync(&limits_watchdog);
        if (restore_limits())
            error = -EIO;
        if (error) {
            mutex_lock(&pou_lock);
            pm_blocked = false;
            mod_delayed_work(system_wq, &limits_watchdog, msecs_to_jiffies(1000));
            mutex_unlock(&pou_lock);
            return NOTIFY_BAD;
        }
    } else if (action == PM_POST_SUSPEND || action == PM_POST_HIBERNATION || action == PM_POST_RESTORE) {
        mutex_lock(&pou_lock);
        pm_blocked = false;
        mutex_unlock(&pou_lock);
    }
    return NOTIFY_OK;
}
static struct notifier_block pou_pm_notifier = { .notifier_call = pou_pm_event };

static int read_integer(const char *method, unsigned long long *value)
{
    return ACPI_FAILURE(acpi_evaluate_integer(NULL, (acpi_string)method, NULL, value)) ? -EIO : 0;
}

static int set_fan(unsigned long long value)
{
    union acpi_object arg = { .type = ACPI_TYPE_INTEGER };
    struct acpi_object_list args = { .count = 1, .pointer = &arg };
    unsigned long long readback;
    arg.integer.value = value;
    if (ACPI_FAILURE(acpi_evaluate_object(NULL, EC "SEFM", &args, NULL)))
        return -EIO;
    if (read_integer(EC "CEFM", &readback) || readback != value)
        return -EIO;
    return 0;
}

static ssize_t fan_profile_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned long long value;
    if (read_integer(EC "CEFM", &value))
        return -EIO;
    return sysfs_emit(buf, "%llu\n", value);
}

static ssize_t fan_profile_store(struct device *dev, struct device_attribute *attr, const char *buf, size_t count)
{
    unsigned int value;
    int error = kstrtouint(buf, 10, &value);
    if (error || value > 1)
        return -EINVAL;
    mutex_lock(&pou_lock);
    // Restore is attempted even if firmware accepted a write but readback fails.
    changed = true;
    error = set_fan(value);
    mutex_unlock(&pou_lock);
    return error ? error : count;
}
static DEVICE_ATTR_RW(fan_profile);

static ssize_t original_fan_profile_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return sysfs_emit(buf, "%llu\n", initial_fan);
}
static DEVICE_ATTR_RO(original_fan_profile);

static ssize_t eco_raw_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned long long value;
    if (read_integer(EC "EC3E", &value))
        return -EIO;
    return sysfs_emit(buf, "%llu\n", value);
}
static DEVICE_ATTR_RO(eco_raw);

static ssize_t power_limits_enabled_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned long long value;
    if (read_integer(EC "EPLE", &value))
        return -EIO;
    return sysfs_emit(buf, "%llu\n", value);
}
static ssize_t power_limits_enabled_store(struct device *dev, struct device_attribute *attr,
                                         const char *buf, size_t count)
{
    unsigned int value;
    unsigned long long readback;
    int error = kstrtouint(buf, 10, &value);
    if (error || value > 1)
        return -EINVAL;
    mutex_lock(&pou_lock);
    if (!value)
        if (!allow_experimental_ec_limits || pm_blocked || unloading) {
            mutex_unlock(&pou_lock);
            return -EOPNOTSUPP;
        }
    if (!value)
        limits_leased = true; /* Also restore after partial ACPI failure. */
    error = set_limits(value);
    if (!error && (read_integer(EC "EPLE", &readback) || readback != value))
        error = -EIO;
    if (value && !error)
        limits_leased = false;
    if (limits_leased) {
        lease_deadline = jiffies + msecs_to_jiffies(15000);
        lease_reason = "active";
        mod_delayed_work(system_wq, &limits_watchdog, msecs_to_jiffies(1000));
    }
    mutex_unlock(&pou_lock);
    return error ? error : count;
}
static DEVICE_ATTR_RW(power_limits_enabled);

static ssize_t performance_lease_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    bool active;
    mutex_lock(&pou_lock);
    active = limits_leased;
    mutex_unlock(&pou_lock);
    return sysfs_emit(buf, "%u\n", active);
}
static ssize_t performance_lease_store(struct device *dev, struct device_attribute *attr,
                                     const char *buf, size_t count)
{
    unsigned int value;
    int temp, error = kstrtouint(buf, 10, &value);
    if (error || value > 1)
        return -EINVAL;
    mutex_lock(&pou_lock);
    if (!value) {
        if (limits_leased)
            lease_reason = "released";
        error = restore_limits_locked();
    } else if (pm_blocked || unloading) {
        error = -EBUSY;
    } else if ((error = package_temperature(&temp))) {
        lease_reason = "sensor";
        restore_limits_locked();
    } else if (temp >= READ_ONCE(lease_cutoff_c)) {
        lease_reason = "temperature";
        restore_limits_locked();
        error = -EAGAIN;
    } else {
        if (!limits_leased) {
            /* A partial ACPI failure must still have an automatic restore. */
            limits_leased = true;
            error = set_limits(0);
        }
        lease_reason = "active";
        lease_deadline = jiffies + msecs_to_jiffies(15000);
        mod_delayed_work(system_wq, &limits_watchdog, msecs_to_jiffies(1000));
    }
    mutex_unlock(&pou_lock);
    return error ? error : count;
}
static DEVICE_ATTR_RW(performance_lease);

static ssize_t performance_lease_version_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return sysfs_emit(buf, "1\n");
}
static DEVICE_ATTR_RO(performance_lease_version);
static ssize_t lease_reason_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    ssize_t result;
    mutex_lock(&pou_lock);
    result = sysfs_emit(buf, "%s\n", lease_reason);
    mutex_unlock(&pou_lock);
    return result;
}
static DEVICE_ATTR_RO(lease_reason);
static ssize_t package_temperature_c_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    int temp, error = package_temperature(&temp);
    return error ? error : sysfs_emit(buf, "%d\n", temp);
}
static DEVICE_ATTR_RO(package_temperature_c);

static ssize_t optical_power_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned long long value;
    if (read_integer("\\_SB.PCI0.SAT0.PRT1.ZPIS", &value))
        return -EIO;
    return sysfs_emit(buf, "%llu\n", value);
}
static ssize_t optical_power_store(struct device *dev, struct device_attribute *attr,
                                  const char *buf, size_t count)
{
    unsigned int value;
    int error = kstrtouint(buf, 10, &value);
    if (error || value > 1)
        return -EINVAL;
    mutex_lock(&pou_lock);
    /* Root helper must fully unbind the optical-only AHCI controller. */
    optical_changed = true;
    error = optical_set_power(value);
    mutex_unlock(&pou_lock);
    return error ? error : count;
}
static DEVICE_ATTR_RW(optical_power);

static ssize_t optical_capabilities_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned long long value;
    if (read_integer("\\SODS", &value))
        return -EIO;
    return sysfs_emit(buf, "%llu\n", value);
}
static DEVICE_ATTR_RO(optical_capabilities);

static ssize_t optical_guard_version_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return sysfs_emit(buf, "1\n");
}
static DEVICE_ATTR_RO(optical_guard_version);

#define POU_RAW_ATTRIBUTE(name, method) \
static ssize_t name##_show(struct device *dev, struct device_attribute *attr, char *buf) \
{ \
    unsigned long long value; \
    if (read_integer(method, &value)) return -EIO; \
    return sysfs_emit(buf, "%llu\n", value); \
} \
static DEVICE_ATTR_RO(name)
POU_RAW_ATTRIBUTE(ec_pl1_raw, "\\_SB.IETM.P589");
POU_RAW_ATTRIBUTE(ec_hot_pl1_raw, "\\_SB.IETM.P590");
POU_RAW_ATTRIBUTE(ec_pl2_raw, "\\_SB.IETM.P592");
POU_RAW_ATTRIBUTE(ec_trip_raw, "\\_SB.IETM.P594");
POU_RAW_ATTRIBUTE(ec_temperature_raw, EC "EC34");
POU_RAW_ATTRIBUTE(manual_fan_supported_raw, "\\_SB.IETM.P015");
POU_RAW_ATTRIBUTE(manual_fan_value_raw, EC "PEN1");

/* MIS2 0243/0244 handlers on CF-SV7 check
 * ER08 bit 1, then read a little-endian pair at ER3E / ER3C. */
static ssize_t battery_word(u8 address, char *buf)
{
    u8 present, high, low, after;
    unsigned int word;

    if (!dmi_match(DMI_PRODUCT_SKU, "CF-SV7HD9VS"))
        return -ENODEV;
    if (ec_read(0x08, &present) || !(present & BIT(1)) ||
        ec_read(address + 1, &high) || ec_read(address, &low) ||
        ec_read(address + 1, &after) || after != high)
        return -ENODATA;
    word = (high << 8) | low;
    if (word == 0xffff || (address == 0x3e && (word < 2231 || word > 3732)))
        return -ENODATA;
    return sysfs_emit(buf, "%u\n", word);
}
static ssize_t battery_charge_index_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return battery_word(0x3c, buf);
}
static DEVICE_ATTR_RO(battery_charge_index);
static ssize_t battery_temp_decikelvin_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return battery_word(0x3e, buf);
}
static DEVICE_ATTR_RO(battery_temp_decikelvin);

static struct attribute *pou_attrs[] = {
    &dev_attr_fan_pwm.attr, &dev_attr_fan_feedback_percent.attr,
    &dev_attr_fan_profile.attr, &dev_attr_original_fan_profile.attr,
    &dev_attr_eco_raw.attr, &dev_attr_power_limits_enabled.attr,
    &dev_attr_optical_power.attr, &dev_attr_optical_capabilities.attr,
    &dev_attr_optical_guard_version.attr, &dev_attr_ec_pl1_raw.attr,
    &dev_attr_ec_hot_pl1_raw.attr, &dev_attr_ec_pl2_raw.attr, &dev_attr_ec_trip_raw.attr,
    &dev_attr_manual_fan_supported_raw.attr, &dev_attr_manual_fan_value_raw.attr,
    &dev_attr_performance_lease.attr, &dev_attr_performance_lease_version.attr,
    &dev_attr_lease_reason.attr, &dev_attr_package_temperature_c.attr,
    &dev_attr_ec_temperature_raw.attr, &dev_attr_battery_charge_index.attr,
    &dev_attr_battery_temp_decikelvin.attr, NULL
};
static const struct attribute_group pou_group = { .attrs = pou_attrs };

static int __init pou_init(void)
{
    int error;
    unsigned int feedback;
    unsigned long long manual;
    acpi_handle handle;
    if (!dmi_match(DMI_PRODUCT_NAME, "CFSV7-3L") || !dmi_match(DMI_BIOS_VERSION, "V3.11L22"))
        return -ENODEV;
    if (ACPI_FAILURE(acpi_get_handle(NULL, EC "SEFM", &handle)))
        return -ENODEV;
    if (read_integer(EC "CEFM", &initial_fan) || initial_fan > 1)
        return -ENODEV;
    fan_available = dmi_match(DMI_PRODUCT_SKU, "CF-SV7HD9VS") &&
        !read_integer("\\_SB.IETM.P015", &manual) && manual == 1 &&
        ACPI_SUCCESS(acpi_get_handle(NULL, EC "TFN1._FSL", &handle)) && !fan_feedback(&feedback);
    pou_device = platform_device_register_simple("pou-firmware", -1, NULL, 0);
    if (IS_ERR(pou_device))
        return PTR_ERR(pou_device);
    error = sysfs_create_group(&pou_device->dev.kobj, &pou_group);
    if (!error)
        error = register_pm_notifier(&pou_pm_notifier);
    if (!error) {
        error = register_reboot_notifier(&fan_reboot_nb);
        if (error)
            unregister_pm_notifier(&pou_pm_notifier);
    }
    if (error)
        platform_device_unregister(pou_device);
    return error;
}

static void __exit pou_exit(void)
{
    sysfs_remove_group(&pou_device->dev.kobj, &pou_group);
    unregister_reboot_notifier(&fan_reboot_nb);
    unregister_pm_notifier(&pou_pm_notifier);
    mutex_lock(&pou_lock);
    unloading = true;
    mutex_unlock(&pou_lock);
    cancel_delayed_work_sync(&limits_watchdog);
    cancel_delayed_work_sync(&fan_watchdog);
    restore_limits();
    mutex_lock(&pou_lock);
    if (fan_release())
        pr_warn("pou_firmware: fan release failed during unload\n");
    if (optical_changed && optical_set_power(1))
        pr_warn("pou_firmware: restoring optical power failed\n");
    if (changed && set_fan(initial_fan))
        pr_warn("pou_firmware: could not restore original fan profile\n");
    mutex_unlock(&pou_lock);
    platform_device_unregister(pou_device);
}
module_init(pou_init);
module_exit(pou_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("AkaseToshiyuki");
MODULE_VERSION("0.7.1");
MODULE_DESCRIPTION("Model-scoped Panasonic firmware fan profile and read-only ECO status");
