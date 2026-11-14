/* SPDX-License-Identifier: MIT */
#include "common.h"
#include "../../src/bootguard/format.h"
static EFI_GUID cpu_guid={0xb08f97ff,0xe6e8,0x4193,{0xa9,0x97,0x5e,0x9e,0x9b,0x0a,0xdb,0x32}};
static EFI_SET_VARIABLE original_set;
static EFI_GET_VARIABLE original_get;
static BOOLEAN mock_secure_boot;
static BOOLEAN deny_candidate,deny_restore;
static BOOLEAN flag(EFI_HANDLE device,CHAR16 *name)
{
    EFI_FILE_HANDLE root=LibOpenRoot(device),file=NULL;EFI_STATUS s;
    if(!root)return FALSE;
    s=uefi_call_wrapper(root->Open,5,root,&file,name,EFI_FILE_MODE_READ,0);
    if(!EFI_ERROR(s))uefi_call_wrapper(file->Close,1,file);
    uefi_call_wrapper(root->Close,1,root);return !EFI_ERROR(s);
}
static EFI_STATUS EFIAPI mock_write(CHAR16 *name,EFI_GUID *guid,UINT32 attrs,UINTN size,VOID *data)
{
    if(size==OMP_CPU_SIZE && data && StrCmp(name,L"CpuSetup")==0) {
        if((deny_candidate && omp_candidate_cpu(data)) || (deny_restore && omp_stock_cpu(data)))return EFI_WRITE_PROTECTED;
    }
    return uefi_call_wrapper(original_set,5,name,guid,attrs,size,data);
}
static EFI_STATUS EFIAPI mock_read(CHAR16 *name,EFI_GUID *guid,UINT32 *attrs,UINTN *size,VOID *data)
{
    if(StrCmp(name,L"SecureBoot")==0) {
        if(*size<1){*size=1;return EFI_BUFFER_TOO_SMALL;}
        *size=1;*(UINT8 *)data=mock_secure_boot?1:0;if(attrs)*attrs=6;return EFI_SUCCESS;
    }
    return uefi_call_wrapper(original_get,5,name,guid,attrs,size,data);
}
static EFI_STATUS dump_cpu(EFI_HANDLE device,UINT8 *data,UINTN size)
{
    EFI_FILE_HANDLE root=LibOpenRoot(device),file=NULL;EFI_STATUS s;
    if(!root)return EFI_NOT_FOUND;
    s=uefi_call_wrapper(root->Open,5,root,&file,L"\\cpu-before.bin",EFI_FILE_MODE_READ|EFI_FILE_MODE_WRITE|EFI_FILE_MODE_CREATE,0);
    if(!EFI_ERROR(s)) {
        s=uefi_call_wrapper(file->Write,3,file,&size,data);
        uefi_call_wrapper(file->Flush,1,file);uefi_call_wrapper(file->Close,1,file);
    }
    uefi_call_wrapper(root->Close,1,root);return s;
}
EFI_STATUS efi_main(EFI_HANDLE image,EFI_SYSTEM_TABLE *table)
{
    EFI_GUID guid=LOADED_IMAGE_PROTOCOL;EFI_LOADED_IMAGE *loaded=NULL,*child_loaded=NULL;
    EFI_STATUS s;EFI_HANDLE child;EFI_DEVICE_PATH *path;UINT8 cpu[OMP_CPU_SIZE]={0};UINTN size=sizeof(cpu),i;UINT32 attrs=0;
    static CHAR16 option[]=L"--guard";
    InitializeLib(image,table);if(!is_vm())return EFI_UNSUPPORTED;
    s=uefi_call_wrapper(BS->HandleProtocol,3,image,&guid,(VOID **)&loaded);if(EFI_ERROR(s))goto done;
    s=uefi_call_wrapper(RT->GetVariable,5,L"CpuSetup",&cpu_guid,&attrs,&size,cpu);
    if(s==EFI_NOT_FOUND) {
        cpu[0x30]=1;cpu[0x227]=1;
        for(i=0;i<3;i++) {
            UINTN offset=0x5b+i*10;
            cpu[offset]=0x60;cpu[offset+1]=0x6d;cpu[offset+4]=0xd8;cpu[offset+5]=0xd6;
        }
        size=flag(loaded->DeviceHandle,L"\\wrong-size.flag")?32:sizeof(cpu);
        s=uefi_call_wrapper(RT->SetVariable,5,L"CpuSetup",&cpu_guid,7,size,cpu);
    }
    if(EFI_ERROR(s))goto done;
    s=dump_cpu(loaded->DeviceHandle,cpu,size);if(EFI_ERROR(s))goto done;
    path=FileDevicePath(loaded->DeviceHandle,L"\\EFI\\OhMyPanasonic35\\guard.efi");
    if(!path){s=EFI_OUT_OF_RESOURCES;goto done;}
    s=uefi_call_wrapper(BS->LoadImage,6,FALSE,image,path,NULL,0,&child);FreePool(path);
    if(EFI_ERROR(s))goto done;
    s=uefi_call_wrapper(BS->HandleProtocol,3,child,&guid,(VOID **)&child_loaded);if(EFI_ERROR(s))goto done;
    if(!flag(loaded->DeviceHandle,L"\\help.flag")) {
        child_loaded->LoadOptions=option;child_loaded->LoadOptionsSize=sizeof(option);
    }
    deny_candidate=flag(loaded->DeviceHandle,L"\\deny-candidate.flag");
    deny_restore=flag(loaded->DeviceHandle,L"\\deny-restore.flag");
    original_set=RT->SetVariable;RT->SetVariable=mock_write;
    mock_secure_boot=flag(loaded->DeviceHandle,L"\\secure-boot.flag");
    original_get=RT->GetVariable;RT->GetVariable=mock_read;
    s=uefi_call_wrapper(BS->StartImage,3,child,NULL,NULL);
done:
    Print(L"Fixture result: %r\r\n",s);finish_vm(EFI_ERROR(s)?0x22:0x10);return s;
}
