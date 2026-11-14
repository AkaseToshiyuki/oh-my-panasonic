/* SPDX-License-Identifier: MIT */
#ifndef OMP_BOOTGUARD_FORMAT_H
#define OMP_BOOTGUARD_FORMAT_H
#include <stdint.h>
#include <stddef.h>

#define OMP_CPU_SIZE 961
#define OMP_FORMAT_VERSION 1
enum omp_phase {
    OMP_ENABLE = 1, OMP_APPLY_PENDING = 2, OMP_BOOT_PENDING = 3,
    OMP_ACTIVE = 4, OMP_DISABLED = 5, OMP_RESTORE = 6, OMP_ERROR = 7
};
struct __attribute__((packed)) omp_backup {
    uint8_t magic[8]; uint32_t version, size;
    char sku[32], bios[32]; uint8_t cpu[OMP_CPU_SIZE]; uint32_t crc;
};
struct __attribute__((packed)) omp_state {
    uint8_t magic[8]; uint32_t version, phase, generation, backup_crc;
    char sku[32], bios[32]; uint32_t crc;
};
_Static_assert(sizeof(struct omp_backup)==1045,"backup ABI");
_Static_assert(sizeof(struct omp_state)==92,"state ABI");

static uint32_t omp_crc32(const void *data,size_t length)
{
    const uint8_t *p=data; uint32_t crc=0xffffffffu; size_t i; unsigned b;
    for(i=0;i<length;i++) {
        crc^=p[i];
        for(b=0;b<8;b++)crc=(crc>>1)^(0xedb88320u & (0u-(crc&1u)));
    }
    return ~crc;
}
static int omp_equal(const void *a,const void *b,size_t size)
{
    const uint8_t *x=a,*y=b;size_t i;
    for(i=0;i<size;i++)if(x[i]!=y[i])return 0;
    return 1;
}
static void omp_copy(void *a,const void *b,size_t size)
{
    uint8_t *x=a;const uint8_t *y=b;size_t i;
    for(i=0;i<size;i++)x[i]=y[i];
}
static int omp_string(const char *fixed,const char *text,size_t size)
{
    size_t i;
    for(i=0;i<size;i++) {
        if(fixed[i]!=text[i])return 0;
        if(!text[i])return 1;
    }
    return 0;
}
static uint32_t omp_u32(const uint8_t *p)
{
    return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
static int omp_identity(const char *sku,const char *bios)
{
    return omp_string(sku,"CF-SR3SKAKS",32) &&
        (omp_string(bios,"V1.00L12",32)||omp_string(bios,"V1.00L27",32));
}
static int omp_stock_cpu(const uint8_t *cpu)
{
    return cpu[0x30]==1 && omp_u32(cpu+0x17)==0 && cpu[0x1b]==0 && cpu[0x227]==1 &&
        omp_u32(cpu+0x5b)==28000 && omp_u32(cpu+0x5f)==55000 &&
        omp_u32(cpu+0x65)==28000 && omp_u32(cpu+0x69)==55000 &&
        omp_u32(cpu+0x6f)==28000 && omp_u32(cpu+0x73)==55000;
}
static int omp_candidate_cpu(const uint8_t *cpu)
{
    return cpu[0x30]==1 && omp_u32(cpu+0x17)==35000 && cpu[0x1b]==1 && cpu[0x227]==0;
}
static void omp_candidate(uint8_t *cpu)
{
    cpu[0x17]=0xb8;cpu[0x18]=0x88;cpu[0x19]=0;cpu[0x1a]=0;
    cpu[0x1b]=1;cpu[0x227]=0;
}
static int omp_owned_equal(const uint8_t *a,const uint8_t *b)
{
    return omp_equal(a+0x17,b+0x17,5) && a[0x227]==b[0x227];
}
static void omp_restore_owned(uint8_t *current,const uint8_t *original)
{
    omp_copy(current+0x17,original+0x17,5);current[0x227]=original[0x227];
}
static int omp_backup_valid(const struct omp_backup *b)
{
    static const uint8_t magic[8]={'O','M','P','3','5','B','1',0};
    return omp_equal(b->magic,magic,8) && b->version==OMP_FORMAT_VERSION && b->size==OMP_CPU_SIZE &&
        omp_identity(b->sku,b->bios) && omp_stock_cpu(b->cpu) &&
        b->crc==omp_crc32(b,offsetof(struct omp_backup,crc));
}
static int omp_state_valid(const struct omp_state *s)
{
    static const uint8_t magic[8]={'O','M','P','3','5','S','1',0};
    return omp_equal(s->magic,magic,8) && s->version==OMP_FORMAT_VERSION &&
        s->phase>=OMP_ENABLE && s->phase<=OMP_ERROR && omp_identity(s->sku,s->bios) &&
        s->crc==omp_crc32(s,offsetof(struct omp_state,crc));
}
#endif
