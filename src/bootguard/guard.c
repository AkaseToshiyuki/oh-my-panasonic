/* SPDX-License-Identifier: MIT */
#include <efi.h>
#include <efilib.h>
#include "format.h"

static EFI_GUID cpu_guid={0xb08f97ff,0xe6e8,0x4193,{0xa9,0x97,0x5e,0x9e,0x9b,0x0a,0xdb,0x32}};
static EFI_GUID global_guid=EFI_GLOBAL_VARIABLE;
static CHAR16 state_path[]=L"\\EFI\\OhMyPanasonic35\\state.bin";
static CHAR16 backup_path[]=L"\\EFI\\OhMyPanasonic35\\original.bin";
static CHAR16 report_path[]=L"\\EFI\\OhMyPanasonic35\\last-boot.txt";
static CHAR8 report[4096];static UINTN used;
static char actual_sku[32],actual_bios[32],actual_product[32];

static VOID log_line(CHAR16 *fmt,...)
{
    CHAR16 line[256];va_list args;UINTN i;
    va_start(args,fmt);VSPrint(line,sizeof(line),fmt,args);va_end(args);
    Print(L"%s",line);
    for(i=0;line[i] && used+1<sizeof(report);i++)report[used++]=line[i]<128?(CHAR8)line[i]:'?';
}

static EFI_STATUS file_io(EFI_HANDLE device,CHAR16 *name,VOID *data,UINTN size,BOOLEAN write,BOOLEAN exclusive)
{
    EFI_FILE_HANDLE root=LibOpenRoot(device),f=NULL;EFI_STATUS s;UINTN n=size;
    UINT8 extra;UINTN one=1;
    if(!root)return EFI_NOT_FOUND;
    if(write && exclusive) {
        s=uefi_call_wrapper(root->Open,5,root,&f,name,EFI_FILE_MODE_READ,0);
        if(!EFI_ERROR(s)){uefi_call_wrapper(f->Close,1,f);s=EFI_ACCESS_DENIED;goto done;}
        if(s!=EFI_NOT_FOUND)goto done;
    }
    s=uefi_call_wrapper(root->Open,5,root,&f,name,
        write?(EFI_FILE_MODE_READ|EFI_FILE_MODE_WRITE|EFI_FILE_MODE_CREATE):EFI_FILE_MODE_READ,0);
    if(EFI_ERROR(s))goto done;
    if(write) {
        EFI_FILE_INFO *info=LibFileInfo(f);
        if(!info)s=EFI_DEVICE_ERROR;
        else {
            info->FileSize=size;
            s=uefi_call_wrapper(f->SetInfo,4,f,&GenericFileInfo,info->Size,info);
            FreePool(info);
        }
        if(!EFI_ERROR(s))s=uefi_call_wrapper(f->Write,3,f,&n,data);
        if(!EFI_ERROR(s))s=uefi_call_wrapper(f->Flush,1,f);
    } else {
        s=uefi_call_wrapper(f->Read,3,f,&n,data);
        if(!EFI_ERROR(s)) {
            s=uefi_call_wrapper(f->Read,3,f,&one,&extra);
            if(!EFI_ERROR(s) && one)s=EFI_BAD_BUFFER_SIZE;
        }
    }
    if(!EFI_ERROR(s) && n!=size)s=EFI_BAD_BUFFER_SIZE;
    uefi_call_wrapper(f->Close,1,f);
done:
    uefi_call_wrapper(root->Close,1,root);return s;
}

static EFI_STATUS read_cpu(UINT8 *data)
{
    UINT32 attrs=0;UINTN size=OMP_CPU_SIZE;
    EFI_STATUS s=uefi_call_wrapper(RT->GetVariable,5,L"CpuSetup",&cpu_guid,&attrs,&size,data);
    if(!EFI_ERROR(s) && (attrs!=7 || size!=OMP_CPU_SIZE))return EFI_COMPROMISED_DATA;
    return s;
}

static EFI_STATUS write_cpu(const UINT8 *data)
{
    UINT8 verify[OMP_CPU_SIZE];EFI_STATUS s;
    s=uefi_call_wrapper(RT->SetVariable,5,L"CpuSetup",&cpu_guid,7,OMP_CPU_SIZE,(VOID *)data);
    if(EFI_ERROR(s))return s;
    s=read_cpu(verify);
    if(!EFI_ERROR(s) && !omp_equal(data,verify,sizeof(verify)))s=EFI_COMPROMISED_DATA;
    SetMem(verify,sizeof(verify),0);return s;
}

static EFI_STATUS save_state(EFI_HANDLE device,struct omp_state *state,UINT32 phase)
{
    struct omp_state verify;EFI_STATUS s;
    state->phase=phase;state->crc=omp_crc32(state,offsetof(struct omp_state,crc));
    s=file_io(device,state_path,state,sizeof(*state),TRUE,FALSE);
    if(!EFI_ERROR(s))s=file_io(device,state_path,&verify,sizeof(verify),FALSE,FALSE);
    if(!EFI_ERROR(s) && !omp_equal(state,&verify,sizeof(verify)))s=EFI_COMPROMISED_DATA;
    log_line(L"state=%u status=0x%lx\r\n",(UINTN)phase,s);return s;
}

static BOOLEAN same_identity(const char *sku,const char *bios)
{
    return omp_equal(sku,actual_sku,32) && omp_equal(bios,actual_bios,32);
}

static BOOLEAN smbios_string(const UINT8 *start,const UINT8 *end,UINT8 index,char out[32])
{
    UINTN n=0;UINT8 i;
    if(!index)return FALSE;
    for(i=1;i<index;i++) {
        while(start<end && *start)start++;
        if(start>=end)return FALSE;
        start++;
    }
    while(start<end && *start) {
        if(n>=31)return FALSE;
        out[n++]=(char)*start++;
    }
    return start<end && n>0;
}

static BOOLEAN machine_identity(void)
{
    EFI_GUID sm3={0xf2fd1544,0x9794,0x4a2c,{0x99,0x2e,0xe5,0xbb,0xcf,0x20,0xe3,0x94}};
    EFI_GUID sm2={0xeb9d2d31,0x2d88,0x11d3,{0x9a,0x16,0x00,0x90,0x27,0x3f,0xc1,0x4d}};
    UINTN i;const UINT8 *base=NULL,*p,*end,*strings,*next;UINTN length=0;BOOLEAN bios=FALSE,product=FALSE,sku=FALSE;
    for(i=0;i<ST->NumberOfTableEntries;i++) {
        EFI_CONFIGURATION_TABLE *t=&ST->ConfigurationTable[i];
        const UINT8 *h=t->VendorTable;
        if(omp_equal(&t->VendorGuid,&sm3,sizeof(sm3)) && omp_equal(h,"_SM3_",5)) {
            UINT64 address;omp_copy(&address,h+16,8);base=(VOID *)(UINTN)address;length=omp_u32(h+12);break;
        }
        if(omp_equal(&t->VendorGuid,&sm2,sizeof(sm2)) && omp_equal(h,"_SM_",4)) {
            base=(VOID *)(UINTN)omp_u32(h+24);length=(UINTN)h[22]|((UINTN)h[23]<<8);
        }
    }
    if(!base || length<6 || length>16*1024*1024)return FALSE;
    end=base+length;p=base;
    while(p+4<=end) {
        UINT8 size=p[1];
        if(size<4 || p+size+2>end)return FALSE;
        strings=p+size;next=strings;
        while(next+1<end && (next[0] || next[1]))next++;
        if(next+1>=end)return FALSE;
        if(p[0]==0 && size>=6)bios=smbios_string(strings,next+1,p[5],actual_bios);
        if(p[0]==1 && size>=0x1b) {
            product=smbios_string(strings,next+1,p[5],actual_product);
            sku=smbios_string(strings,next+1,p[0x19],actual_sku);
        }
        if(p[0]==127)break;
        p=next+2;
    }
    return bios && product && sku && omp_string(actual_product,"CFSR3-1",32) && omp_identity(actual_sku,actual_bios);
}

static BOOLEAN secure_boot_disabled(void)
{
    UINT8 enabled=1;UINTN size=1;UINT32 attrs=0;
    EFI_STATUS s=uefi_call_wrapper(RT->GetVariable,5,L"SecureBoot",&global_guid,&attrs,&size,&enabled);
    log_line(L"secure_boot_read=0x%lx value=%u\r\n",s,(UINTN)enabled);
    return !EFI_ERROR(s) && size==1 && enabled==0;
}

static EFI_STATUS arm_next(void)
{
    UINT16 current,next;UINTN size=2;UINT32 attrs=0;EFI_STATUS s;
    s=uefi_call_wrapper(RT->GetVariable,5,L"BootCurrent",&global_guid,&attrs,&size,&current);
    if(EFI_ERROR(s)||size!=2)return EFI_NOT_FOUND;
    size=2;s=uefi_call_wrapper(RT->GetVariable,5,L"BootNext",&global_guid,&attrs,&size,&next);
    if(!EFI_ERROR(s) && (size!=2||next!=current))return EFI_ACCESS_DENIED;
    if(EFI_ERROR(s) && s!=EFI_NOT_FOUND)return s;
    return uefi_call_wrapper(RT->SetVariable,5,L"BootNext",&global_guid,7,2,&current);
}

static BOOLEAN recover(EFI_HANDLE device,struct omp_state *state,const struct omp_backup *backup,UINT8 *current)
{
    UINT8 expected[OMP_CPU_SIZE];UINTN i;BOOLEAN was_changed;EFI_STATUS s;
    if(current[0x30]!=1)return FALSE;
    omp_copy(expected,backup->cpu,OMP_CPU_SIZE);omp_candidate(expected);
    for(i=0x17;i<=0x1b;i++)if(current[i]!=expected[i] && current[i]!=backup->cpu[i])return FALSE;
    if(current[0x227]!=expected[0x227] && current[0x227]!=backup->cpu[0x227])return FALSE;
    was_changed=!omp_owned_equal(current,backup->cpu);
    if(EFI_ERROR(save_state(device,state,OMP_RESTORE)))return FALSE;
    omp_restore_owned(current,backup->cpu);
    s=was_changed?write_cpu(current):EFI_SUCCESS;
    log_line(L"restore_status=0x%lx changed=%u\r\n",s,(UINTN)was_changed);
    if(EFI_ERROR(s))return FALSE;
    if(EFI_ERROR(save_state(device,state,OMP_DISABLED)))return FALSE;
    if(was_changed) {
        file_io(device,report_path,report,used,TRUE,FALSE);
        uefi_call_wrapper(RT->ResetSystem,4,EfiResetCold,EFI_SUCCESS,0,NULL);
        log_line(L"restore_reset_returned\r\n");return FALSE;
    }
    return TRUE;
}

static VOID transaction(EFI_HANDLE device)
{
    struct omp_state state={0};struct omp_backup backup={0},verify_backup;
    UINT8 cpu[OMP_CPU_SIZE];EFI_STATUS ss,bs,s;BOOLEAN state_ok,backup_ok;
    state_ok=machine_identity();
    log_line(L"identity=%u sku=%a bios=%a product=%a\r\n",(UINTN)state_ok,actual_sku,actual_bios,actual_product);
    if(!state_ok || !secure_boot_disabled()) {log_line(L"unsupported_identity_or_secure_boot\r\n");return;}
    ss=file_io(device,state_path,&state,sizeof(state),FALSE,FALSE);
    bs=file_io(device,backup_path,&backup,sizeof(backup),FALSE,FALSE);
    state_ok=!EFI_ERROR(ss) && omp_state_valid(&state) && same_identity(state.sku,state.bios);
    backup_ok=!EFI_ERROR(bs) && omp_backup_valid(&backup) && same_identity(backup.sku,backup.bios);
    s=read_cpu(cpu);if(EFI_ERROR(s)){log_line(L"cpu_read=0x%lx\r\n",s);return;}
    if(!state_ok) {
        if(backup_ok && omp_candidate_cpu(cpu)) {
            omp_copy(state.magic,"OMP35S1",8);state.version=1;state.backup_crc=backup.crc;
            omp_copy(state.sku,actual_sku,32);omp_copy(state.bios,actual_bios,32);
            log_line(L"invalid_state_recovery\r\n");recover(device,&state,&backup,cpu);
        } else log_line(L"invalid_state_no_write\r\n");
        return;
    }
    log_line(L"entry_phase=%u generation=%u\r\n",(UINTN)state.phase,(UINTN)state.generation);
    if(state.phase==OMP_DISABLED)return;
    if(state.phase==OMP_ENABLE) {
        if(!omp_stock_cpu(cpu)) {log_line(L"original_profile_rejected\r\n");return;}
        if(bs==EFI_NOT_FOUND) {
            omp_copy(backup.magic,"OMP35B1",8);backup.version=1;backup.size=OMP_CPU_SIZE;
            omp_copy(backup.sku,actual_sku,32);omp_copy(backup.bios,actual_bios,32);
            omp_copy(backup.cpu,cpu,OMP_CPU_SIZE);backup.crc=omp_crc32(&backup,offsetof(struct omp_backup,crc));
            s=file_io(device,backup_path,&backup,sizeof(backup),TRUE,TRUE);
            if(!EFI_ERROR(s))s=file_io(device,backup_path,&verify_backup,sizeof(backup),FALSE,FALSE);
            if(EFI_ERROR(s)||!omp_equal(&backup,&verify_backup,sizeof(backup))) {log_line(L"backup_failed\r\n");return;}
            backup_ok=TRUE;
        }
        if(!backup_ok || !omp_owned_equal(cpu,backup.cpu)) {log_line(L"backup_mismatch\r\n");return;}
        state.backup_crc=backup.crc;
        if(EFI_ERROR(arm_next()) || EFI_ERROR(save_state(device,&state,OMP_APPLY_PENDING)))return;
        omp_candidate(cpu);s=write_cpu(cpu);log_line(L"candidate_write=0x%lx\r\n",s);
        if(EFI_ERROR(s)) {
            if(!EFI_ERROR(read_cpu(cpu)))recover(device,&state,&backup,cpu);
            return;
        }
        file_io(device,report_path,report,used,TRUE,FALSE);
        uefi_call_wrapper(RT->ResetSystem,4,EfiResetCold,EFI_SUCCESS,0,NULL);
        if(!EFI_ERROR(read_cpu(cpu)))recover(device,&state,&backup,cpu);
        return;
    }
    if(!backup_ok || state.backup_crc!=backup.crc) {log_line(L"backup_invalid_no_write\r\n");return;}
    if(state.phase==OMP_RESTORE || state.phase==OMP_BOOT_PENDING || state.phase==OMP_ERROR) {
        log_line(L"recovery_requested\r\n");recover(device,&state,&backup,cpu);return;
    }
    if(state.phase==OMP_ACTIVE || state.phase==OMP_APPLY_PENDING) {
        if(!omp_candidate_cpu(cpu)) {
            if(omp_owned_equal(cpu,backup.cpu))save_state(device,&state,OMP_DISABLED);
            else log_line(L"external_configuration_no_write\r\n");
            return;
        }
        if(state.generation==0xffffffffu) {recover(device,&state,&backup,cpu);return;}
        state.generation++;
        if(EFI_ERROR(save_state(device,&state,OMP_BOOT_PENDING))) {recover(device,&state,&backup,cpu);return;}
        log_line(L"awaiting_linux_health_confirmation\r\n");
    }
    SetMem(cpu,sizeof(cpu),0);SetMem(&backup,sizeof(backup),0);
}

EFI_STATUS efi_main(EFI_HANDLE image,EFI_SYSTEM_TABLE *table)
{
    EFI_LOADED_IMAGE *loaded=NULL;EFI_GUID guid=LOADED_IMAGE_PROTOCOL;EFI_STATUS s;
    EFI_DEVICE_PATH *path;EFI_HANDLE child=NULL;static CHAR16 option[]=L"--guard";
    InitializeLib(image,table);
    s=uefi_call_wrapper(BS->HandleProtocol,3,image,&guid,(VOID **)&loaded);if(EFI_ERROR(s))return s;
    if(loaded->LoadOptionsSize!=sizeof(option) || !loaded->LoadOptions || !omp_equal(loaded->LoadOptions,option,sizeof(option))) {
        Print(L"Oh My Panasonic boot power guard. Requires configured --guard entry.\r\n");return EFI_SUCCESS;
    }
    log_line(L"Oh My Panasonic boot power guard v1\r\n");
    transaction(loaded->DeviceHandle);
    file_io(loaded->DeviceHandle,report_path,report,used,TRUE,FALSE);
    path=FileDevicePath(loaded->DeviceHandle,L"\\EFI\\ubuntu\\shimx64.efi");
    if(!path)return EFI_OUT_OF_RESOURCES;
    s=uefi_call_wrapper(BS->LoadImage,6,FALSE,image,path,NULL,0,&child);FreePool(path);
    if(!EFI_ERROR(s))s=uefi_call_wrapper(BS->StartImage,3,child,NULL,NULL);
    return s;
}
