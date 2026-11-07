/* SPDX-License-Identifier: GPL-2.0-only */
/* CF-SV7HD9VS L22: TFN1 sets a requested percentage, B5/81 supplies
 * separate, ramped EC feedback. All ownership state is under pou_lock. */
static bool fan_available, fan_owned;
static unsigned int fan_target;
static unsigned long fan_deadline;
static void fan_watch(struct work_struct *work);
static DECLARE_DELAYED_WORK(fan_watchdog, fan_watch);

static int fan_feedback(unsigned int *percent)
{
    union acpi_object args[4] = {};
    const u8 values[] = {0xb5, 0x81, 0, 0};
    struct acpi_object_list list = { .count = 4, .pointer = args };
    unsigned long long value;
    int i;

    for (i = 0; i < ARRAY_SIZE(values); i++) {
        args[i].type = ACPI_TYPE_INTEGER;
        args[i].integer.value = values[i];
    }
    if (ACPI_FAILURE(acpi_evaluate_integer(NULL, EC "EC88", &list, &value)) ||
        (value & 0xffffff) || value >> 24 > 100)
        return -EIO;
    *percent = value >> 24;
    return 0;
}

static int fan_write_percent(unsigned int percent)
{
    acpi_handle fan;
    unsigned long long actual;
    if (ACPI_FAILURE(acpi_get_handle(NULL, EC "TFN1", &fan)) ||
        ACPI_FAILURE(acpi_execute_simple_method(fan, "_FSL", percent)) ||
        ACPI_FAILURE(acpi_evaluate_integer(NULL, EC "PEN1", NULL, &actual)) || actual != percent)
        return -EIO;
    return 0;
}

static int fan_release(void)
{
    unsigned long long actual;
    if (!fan_owned)
        return 0;
    if (ACPI_FAILURE(acpi_evaluate_integer(NULL, EC "PEN1", NULL, &actual)))
        return -EIO;
    /* A thermal driver that has taken ownership keeps its own request. */
    if (actual == fan_target && fan_write_percent(0))
        return -EIO;
    fan_owned = false;
    fan_target = 0;
    return 0;
}

static void fan_watch(struct work_struct *work)
{
    int temp;
    mutex_lock(&pou_lock);
    if (fan_owned && (unloading || pm_blocked || time_after_eq(jiffies, fan_deadline) ||
        package_temperature(&temp) || (temp >= 85 && fan_target < 100)))
        fan_release();
    if (fan_owned && !unloading)
        schedule_delayed_work(&fan_watchdog, HZ);
    mutex_unlock(&pou_lock);
}

static ssize_t fan_feedback_percent_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    unsigned int percent;
    int err;
    if (!fan_available)
        return -EOPNOTSUPP;
    mutex_lock(&pou_lock);
    err = fan_feedback(&percent);
    mutex_unlock(&pou_lock);
    return err ? err : sysfs_emit(buf, "%u\n", percent);
}
static DEVICE_ATTR_RO(fan_feedback_percent);

static ssize_t fan_pwm_show(struct device *dev, struct device_attribute *attr, char *buf)
{
    return fan_available ? sysfs_emit(buf, "%u\n", READ_ONCE(fan_target)) : -EOPNOTSUPP;
}

static ssize_t fan_pwm_store(struct device *dev, struct device_attribute *attr, const char *buf, size_t count)
{
    unsigned int percent;
    unsigned long long current_level;
    int temp, err = kstrtouint(buf, 10, &percent);
    if (err || percent > 100 || (percent && percent < 20) || (percent & 1))
        return -EINVAL;
    if (!fan_available)
        return -EOPNOTSUPP;
    mutex_lock(&pou_lock);
    if (!percent) {
        err = fan_release();
        goto done;
    }
    if (unloading || pm_blocked || package_temperature(&temp) || (temp >= 85 && percent < 100)) {
        fan_release();
        err = -EAGAIN;
        goto done;
    }
    if (ACPI_FAILURE(acpi_evaluate_integer(NULL, EC "PEN1", NULL, &current_level))) {
        err = -EIO;
        goto done;
    }
    if (current_level != fan_target) {
        fan_owned = false;
        fan_target = 0;
        err = -EBUSY;
        goto done;
    }
    fan_deadline = jiffies + 8 * HZ;
    if (!fan_owned || percent != fan_target) {
        fan_owned = true;
        fan_target = percent;
        err = fan_write_percent(percent);
        if (err)
            fan_deadline = jiffies;
    }
    if (!delayed_work_pending(&fan_watchdog))
        schedule_delayed_work(&fan_watchdog, HZ);
done:
    mutex_unlock(&pou_lock);
    return err ? err : count;
}
static DEVICE_ATTR_RW(fan_pwm);

static int fan_shutdown(struct notifier_block *nb, unsigned long action, void *unused)
{
    mutex_lock(&pou_lock);
    pm_blocked = true;
    fan_release();
    mutex_unlock(&pou_lock);
    return NOTIFY_DONE;
}
static struct notifier_block fan_reboot_nb = {.notifier_call = fan_shutdown};
