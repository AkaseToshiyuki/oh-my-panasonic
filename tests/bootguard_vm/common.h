/* Test fixtures: only execute in an EDK II virtual machine. */
#include <efi.h>
#include <efilib.h>
static BOOLEAN is_vm(VOID)
{
    UINT32 a,b,c,d;
    __asm__ volatile("cpuid":"=a"(a),"=b"(b),"=c"(c),"=d"(d):"a"(1),"c"(0));
    if(!(c & (1U<<31))) return FALSE;
    __asm__ volatile("cpuid":"=a"(a),"=b"(b),"=c"(c),"=d"(d):"a"(0x40000000),"c"(0));
    return b==0x4b4d564b && c==0x564b4d56 && d==0x4d;
}
static VOID finish_vm(UINT32 result)
{
    if(is_vm()) __asm__ volatile("outl %0,%1"::"a"(result),"Nd"((UINT16)0xf4));
}
static EFI_STATUS marker(EFI_HANDLE device,CHAR16 *name)
{
    EFI_FILE_HANDLE root=LibOpenRoot(device),f=NULL;
    EFI_STATUS s;
    UINTN n=2;
    if(!root)return EFI_NOT_FOUND;
    s=uefi_call_wrapper(root->Open,5,root,&f,name,EFI_FILE_MODE_READ|EFI_FILE_MODE_WRITE|EFI_FILE_MODE_CREATE,0);
    if(!EFI_ERROR(s)) {
        s=uefi_call_wrapper(f->Write,3,f,&n,"OK");
        uefi_call_wrapper(f->Flush,1,f);
        uefi_call_wrapper(f->Close,1,f);
    }
    uefi_call_wrapper(root->Close,1,root);
    return s;
}
