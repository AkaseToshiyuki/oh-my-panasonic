// SPDX-License-Identifier: GPL-2.0-only
/* Validated CFSR3-1 firmware: native TFN1 requests with an expiring lease.
 * CEFM=1 enables OS active cooling; it is not the OEM Quiet policy enum.
 * Without a live lease, CEFM=0 returns cooling to firmware. */
#include <linux/acpi.h>
#include <linux/dmi.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/reboot.h>
#include <linux/suspend.h>
#include <linux/workqueue.h>
#include <asm/msr.h>
#include "pou_models.h"

#define EC "\\_SB.PC00.LPCB.EC0."
static struct platform_device *pou_device;
static DEFINE_MUTEX(pou_lock);
static unsigned long long original, selected;
static bool owned, blocked, unloading, manual_owned;
static unsigned int target;
static unsigned long deadline;
static void watch(struct work_struct *work);
static DECLARE_DELAYED_WORK(watchdog, watch);

static int read_profile(unsigned long long *value)
{
    if (ACPI_FAILURE(acpi_evaluate_integer(NULL, EC "CEFM", NULL, value)) || *value > 1)
        return -EIO;
    return 0;
}

static int set_profile(unsigned int value)
{
    unsigned long long actual;
    acpi_handle handle;
    if (ACPI_FAILURE(acpi_get_handle(NULL, EC "SEFM", &handle)) ||
        ACPI_FAILURE(acpi_execute_simple_method(handle, NULL, value)) ||
        read_profile(&actual) || actual != value)
        return -EIO;
    return 0;
}

static int temperature(int *celsius)
{
    u64 tjmax, status;
    unsigned int maximum;
    if (rdmsrq_safe(MSR_IA32_TEMPERATURE_TARGET, &tjmax) ||
        rdmsrq_safe(MSR_IA32_PACKAGE_THERM_STATUS, &status) || !(status & BIT_ULL(31)))
        return -EIO;
    maximum = (tjmax >> 16) & 0xff;
    if (maximum < 80 || maximum > 110)
        return -EIO;
    *celsius = maximum - ((status >> 16) & 0x7f);
    return 0;
}

static int requested(unsigned long long *value)
{
    return ACPI_FAILURE(acpi_evaluate_integer(NULL, EC "PEN1", NULL, value)) ? -EIO : 0;
}

static int output(unsigned int value)
{
    acpi_handle handle;
    unsigned long long actual;
    if (ACPI_FAILURE(acpi_get_handle(NULL, EC "TFN1", &handle)) ||
        ACPI_FAILURE(acpi_execute_simple_method(handle, "_FSL", value)) ||
        requested(&actual) || actual != value)
        return -EIO;
    return 0;
}

static int release(void)
{
    unsigned long long actual;
    int err;
    if (!manual_owned)
        return 0;
    if (requested(&actual))
        return -EIO;
    if (actual != target) {
        /* Do not clear a cooling request taken over by another thermal driver. */
        manual_owned = false;
        target = 0;
        return 0;
    }
    err = set_profile(0);
    if (!err)
        err = output(0);
    if (!err) {
        manual_owned = false;
        target = 0;
    }
    return err;
}

static void watch(struct work_struct *work)
{
    int temp;
    mutex_lock(&pou_lock);
    if (manual_owned && (blocked || unloading || time_after_eq(jiffies, deadline) ||
        temperature(&temp) || temp >= 85)) {
        if (release())
            pr_err_ratelimited("pou_sr3_fan: firmware restoration failed; retrying\n");
    }
    if (manual_owned && !unloading)
        schedule_delayed_work(&watchdog, HZ);
    mutex_unlock(&pou_lock);
}

static ssize_t fan_feedback_percent_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    union acpi_object args[4] = {};
    const u8 bytes[4] = {0xb5, 0x81, 0, 0};
    struct acpi_object_list list = {.count = 4, .pointer = args};
    unsigned long long value;
    acpi_status status;
    int i;
    for (i = 0; i < 4; i++) {
        args[i].type = ACPI_TYPE_INTEGER;
        args[i].integer.value = bytes[i];
    }
    mutex_lock(&pou_lock);
    status = acpi_evaluate_integer(NULL, EC "EC88", &list, &value);
    mutex_unlock(&pou_lock);
    if (ACPI_FAILURE(status) || (value & 0xffffff) || value >> 24 > 100)
        return -EIO;
    return sysfs_emit(buf, "%llu\n", value >> 24);
}
static DEVICE_ATTR_RO(fan_feedback_percent);

static ssize_t package_temperature_c_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    int temp, err = temperature(&temp);
    return err ? err : sysfs_emit(buf, "%d\n", temp);
}
static DEVICE_ATTR_RO(package_temperature_c);

static ssize_t software_quiet_curve_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return sysfs_emit(buf, "1\n");
}
static DEVICE_ATTR_RO(software_quiet_curve);

static ssize_t fan_pwm_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return sysfs_emit(buf, "%u\n", READ_ONCE(target));
}

static ssize_t fan_pwm_store(struct device *dev, struct device_attribute *attr, const char *buf, size_t count)
{
    unsigned int value;
    unsigned long long actual, profile;
    int temp, err = kstrtouint(buf, 10, &value);
    if (err || value > 100 || (value && value < 20) || value & 1)
        return -EINVAL;
    mutex_lock(&pou_lock);
    if (!value) {
        err = release();
        goto done;
    }
    if (blocked || unloading || temperature(&temp) || temp >= 85) {
        release();
        err = -EAGAIN;
        goto done;
    }
    if (requested(&actual) || read_profile(&profile)) {
        err = -EIO;
        goto done;
    }
    if (actual != target || (manual_owned && profile != 1)) {
        manual_owned = false;
        target = 0;
        err = -EBUSY;
        goto done;
    }
    deadline = jiffies + 8 * HZ;
    if (!manual_owned || target != value) {
        manual_owned = true;
        target = value;
        /* Establish output before switching from firmware to OS cooling. */
        err = output(value);
        if (!err)
            err = set_profile(1);
        if (err)
            deadline = jiffies;
    }
    mod_delayed_work(system_wq, &watchdog, HZ);
done:
    mutex_unlock(&pou_lock);
    return err ? err : count;
}
static DEVICE_ATTR_RW(fan_pwm);

static ssize_t fan_profile_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned long long value;
    int err;
    mutex_lock(&pou_lock);
    err = read_profile(&value);
    if (!err)
        value = selected;
    mutex_unlock(&pou_lock);
    return err ? err : sysfs_emit(buf, "%llu\n", value);
}

static ssize_t fan_profile_store(struct device *dev, struct device_attribute *attr,
                                 const char *buf, size_t count)
{
    unsigned int value;
    int err = kstrtouint(buf, 10, &value);
    if (err || value > 1)
        return -EINVAL;
    mutex_lock(&pou_lock);
    if (blocked) {
        err = -EBUSY;
    } else {
        owned = true;
        err = release();
        if (!err)
            err = set_profile(0);
        if (err) {
            if (!set_profile(original))
                owned = false;
        } else {
            selected = value;
        }
    }
    mutex_unlock(&pou_lock);
    return err ? err : count;
}
static DEVICE_ATTR_RW(fan_profile);

static ssize_t original_fan_profile_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return sysfs_emit(buf, "%llu\n", original);
}
static DEVICE_ATTR_RO(original_fan_profile);

static struct attribute *attrs[] = {
    &dev_attr_fan_profile.attr, &dev_attr_original_fan_profile.attr,
    &dev_attr_fan_feedback_percent.attr, &dev_attr_fan_pwm.attr,
    &dev_attr_package_temperature_c.attr, &dev_attr_software_quiet_curve.attr, NULL
};
static const struct attribute_group group = { .attrs = attrs };

static int sleep_event(struct notifier_block *nb, unsigned long event, void *unused)
{
    int err = 0;
    mutex_lock(&pou_lock);
    if (event == PM_SUSPEND_PREPARE || event == PM_HIBERNATION_PREPARE) {
        blocked = true;
        err = release();
        if (!err && owned)
            err = set_profile(0);
        if (err)
            blocked = false;
    } else if (event == PM_POST_SUSPEND || event == PM_POST_HIBERNATION || event == PM_POST_RESTORE) {
        blocked = false;
        if (owned && set_profile(0))
            pr_warn("pou_sr3_fan: could not restore selected profile after resume\n");
    }
    mutex_unlock(&pou_lock);
    return err ? NOTIFY_BAD : NOTIFY_OK;
}
static struct notifier_block pm_nb = { .notifier_call = sleep_event };

static int shutdown_event(struct notifier_block *nb, unsigned long event, void *unused)
{
    mutex_lock(&pou_lock);
    blocked = true;
    if (release() || (owned && set_profile(0)))
        pr_warn("pou_sr3_fan: could not restore boot profile before shutdown\n");
    mutex_unlock(&pou_lock);
    return NOTIFY_DONE;
}
static struct notifier_block reboot_nb = { .notifier_call = shutdown_event };

static int __init pou_init(void)
{
    acpi_handle handle;
    int err;
    if (!pou_sr3_supported())
        return -ENODEV;
    if (ACPI_FAILURE(acpi_get_handle(NULL, EC "SEFM", &handle)) ||
        read_profile(&original))
        return -ENODEV;
    /* Firmware automatic cooling is the safe handoff, even if a previous
     * userspace service left OS active cooling selected. */
    original = 0;
    selected = original;
    {
        unsigned long long fine, level;
        int temp;
        if (ACPI_FAILURE(acpi_evaluate_integer(NULL, "\\_SB.IETM.P015", NULL, &fine)) ||
            fine != 1 || requested(&level) || level != 0 || temperature(&temp))
            return -ENODEV;
    }
    pou_device = platform_device_register_simple("pou-firmware", -1, NULL, 0);
    if (IS_ERR(pou_device))
        return PTR_ERR(pou_device);
    err = sysfs_create_group(&pou_device->dev.kobj, &group);
    if (err)
        goto fail;
    err = register_pm_notifier(&pm_nb);
    if (err)
        goto fail;
    err = register_reboot_notifier(&reboot_nb);
    if (err) {
        unregister_pm_notifier(&pm_nb);
        goto fail;
    }
    return 0;
fail:
    platform_device_unregister(pou_device);
    return err;
}

static void __exit pou_exit(void)
{
    sysfs_remove_group(&pou_device->dev.kobj, &group);
    unregister_reboot_notifier(&reboot_nb);
    unregister_pm_notifier(&pm_nb);
    mutex_lock(&pou_lock);
    unloading = true;
    mutex_unlock(&pou_lock);
    cancel_delayed_work_sync(&watchdog);
    shutdown_event(NULL, 0, NULL);
    platform_device_unregister(pou_device);
}
module_init(pou_init);
module_exit(pou_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("AkaseToshiyuki");
MODULE_VERSION("0.2.2");
MODULE_DESCRIPTION("Guarded native fan control for validated Panasonic CF-SR3");
