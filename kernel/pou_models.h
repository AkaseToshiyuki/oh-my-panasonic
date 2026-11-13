/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POU_MODELS_H
#define POU_MODELS_H
#include <linux/dmi.h>

static inline bool pou_sr3_supported(void)
{
    return dmi_match(DMI_PRODUCT_NAME, "CFSR3-1") &&
           dmi_match(DMI_PRODUCT_SKU, "CF-SR3SKAKS") &&
           (dmi_match(DMI_BIOS_VERSION, "V1.00L12") ||
            dmi_match(DMI_BIOS_VERSION, "V1.00L27"));
}
#endif
