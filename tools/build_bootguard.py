#!/usr/bin/env python3
"""Build the optional x86-64 boot guard using GNU-EFI."""
from pathlib import Path
import argparse
import os
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def build_image(source,output,name,sdk=None,extra=()):
    sdk=Path(sdk or os.environ.get('GNU_EFI_ROOT','/usr'))
    output.mkdir(parents=True,exist_ok=True)
    obj=output/(name+'.o');shared=output/(name+'.so');efi=output/(name+'.efi')
    lib=sdk/'lib'
    if not (lib/'crt0-efi-x86_64.o').is_file():
        lib=sdk/'lib/x86_64-linux-gnu'
    if not (lib/'crt0-efi-x86_64.o').is_file():
        raise RuntimeError('GNU-EFI development files are required')
    subprocess.run(['cc','-I'+str(sdk/'include/efi'),'-I'+str(sdk/'include/efi/x86_64'),
        '-DGNU_EFI_USE_MS_ABI','-fpic','-fshort-wchar','-mno-red-zone','-mno-avx',
        '-maccumulate-outgoing-args','-fno-stack-protector','-fno-strict-aliasing',
        '-Wall','-Wextra','-Werror','-O2',*extra,'-c',str(source),'-o',str(obj)],check=True)
    subprocess.run(['ld','-nostdlib','-znocombreloc','-T',str(lib/'elf_x86_64_efi.lds'),
        '-shared','-Bsymbolic',str(lib/'crt0-efi-x86_64.o'),str(obj),'-L'+str(lib),
        '-lefi','-lgnuefi','-o',str(shared)],check=True)
    subprocess.run(['objcopy',*[x for section in ('.text','.sdata','.data','.rodata','.dynamic','.dynsym','.rel','.rela','.reloc') for x in ('-j',section)],
        '--output-target=efi-app-x86_64',str(shared),str(efi)],check=True)
    return efi

def build(sdk=None):
    sdk=Path(sdk or os.environ.get('GNU_EFI_ROOT','/usr'))
    copyright=sdk/'share/doc/gnu-efi/copyright'
    if not copyright.is_file():raise RuntimeError('GNU-EFI copyright notices are required')
    image=build_image(ROOT/'src/bootguard/guard.c',ROOT/'build','pou-bootguard',sdk)
    shutil.copyfile(copyright,ROOT/'build/gnu-efi-copyright')
    return image

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--sdk')
    print(build(parser.parse_args().sdk))
