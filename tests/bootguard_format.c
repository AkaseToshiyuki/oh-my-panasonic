/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <string.h>
#include "../src/bootguard/format.h"
int main(void)
{
    struct omp_backup b={0};struct omp_state s={0};uint8_t changed[OMP_CPU_SIZE];unsigned i;
    memcpy(b.magic,"OMP35B1",8);b.version=1;b.size=OMP_CPU_SIZE;
    strcpy(b.sku,"CF-SR3SKAKS");strcpy(b.bios,"V1.00L27");
    b.cpu[0x30]=1;b.cpu[0x227]=1;
    for(i=0;i<3;i++) {
        unsigned offset=0x5b+i*10;
        b.cpu[offset]=0x60;b.cpu[offset+1]=0x6d;
        b.cpu[offset+4]=0xd8;b.cpu[offset+5]=0xd6;
    }
    b.crc=omp_crc32(&b,offsetof(struct omp_backup,crc));assert(omp_backup_valid(&b));
    memcpy(changed,b.cpu,sizeof(changed));omp_candidate(changed);assert(omp_candidate_cpu(changed));
    assert(!omp_stock_cpu(changed));assert(!omp_owned_equal(changed,b.cpu));
    changed[0x100]=73;omp_restore_owned(changed,b.cpu);
    assert(omp_owned_equal(changed,b.cpu));assert(omp_stock_cpu(changed));assert(changed[0x100]==73);
    b.cpu[10]^=1;assert(!omp_backup_valid(&b));b.cpu[10]^=1;
    b.cpu[0x30]=0;b.crc=omp_crc32(&b,offsetof(struct omp_backup,crc));assert(!omp_backup_valid(&b));
    memcpy(s.magic,"OMP35S1",8);s.version=1;s.phase=OMP_ENABLE;
    strcpy(s.sku,"CF-SR3SKAKS");strcpy(s.bios,"V1.00L12");
    s.crc=omp_crc32(&s,offsetof(struct omp_state,crc));assert(omp_state_valid(&s));
    s.phase=0;s.crc=omp_crc32(&s,offsetof(struct omp_state,crc));assert(!omp_state_valid(&s));
    assert(!omp_identity("CF-SV7HD9VS","V1.00L27"));
    assert(!omp_identity("CF-SR3SKAKS","V1.00L28"));
    assert(omp_crc32("123456789",9)==0xcbf43926u);
    return 0;
}
