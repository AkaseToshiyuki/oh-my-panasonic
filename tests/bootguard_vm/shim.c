#include "common.h"
EFI_STATUS efi_main(EFI_HANDLE image,EFI_SYSTEM_TABLE *table)
{
    EFI_LOADED_IMAGE *loaded=NULL;
    EFI_GUID guid=LOADED_IMAGE_PROTOCOL;
    EFI_STATUS s;
    InitializeLib(image,table);
    if(!is_vm())return EFI_UNSUPPORTED;
    s=uefi_call_wrapper(BS->HandleProtocol,3,image,&guid,(VOID **)&loaded);
    if(!EFI_ERROR(s))s=marker(loaded->DeviceHandle,L"\\EFI\\OhMyPanasonic35\\chain.ok");
    finish_vm(EFI_ERROR(s)?0x21:0x10);
    return s;
}
