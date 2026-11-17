/** @file
  Publish the FAT initrd from a Mu-Silicium FDT handoff as a read-only block device.

  SPDX-License-Identifier: BSD-2-Clause
**/

#include <Uefi.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/FdtLib.h>
#include <Library/HobLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/PcdLib.h>
#include <Library/PrintLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include <Guid/FileInfo.h>
#include <Pi/PiFirmwareFile.h>
#include <Protocol/BlockIo.h>
#include <Protocol/Cpu.h>
#include <Protocol/DebugSupport.h>
#include <Protocol/DevicePath.h>
#include <Protocol/FirmwareVolume2.h>
#include <Protocol/LoadFile2.h>
#include <Protocol/LoadedImage.h>
#include <Protocol/SimpleFileSystem.h>
#include <Protocol/UniXNULoader.h>

// Mu-Silicium SEC stores the incoming x0 value in this HOB as a UINT64.
STATIC CONST EFI_GUID mEntryArgHobGuid = {
  0xDAEE82E6, 0x8090, 0x4E72, { 0xBF, 0xB9, 0x78, 0xC1, 0x3C, 0xD4, 0xE3, 0x59 }
};

STATIC EFI_GUID mFileInfoGuid = EFI_FILE_INFO_ID;

STATIC UINT8  *mAppImage;
STATIC UINTN  mAppImageSize;
STATIC UINT64 mAppBase;
STATIC BOOLEAN mLaunched = FALSE;

STATIC
UINT64
ReadCells (
  IN CONST VOID  *Data,
  IN UINT32      Cells
  )
{
  if (Cells == 2) {
    return Fdt64ToCpu (ReadUnaligned64 (Data));
  }

  return Fdt32ToCpu (ReadUnaligned32 (Data));
}

#define RAM_BLOCK_SIZE          512
#define FAT_SIGNATURE_OFFSET    0x1FE
#define FAT_LABEL_OFFSET        0x47
#define FAT_LABEL_LENGTH        8

#pragma pack(1)
typedef struct {
  VENDOR_DEVICE_PATH          Vendor;
  EFI_DEVICE_PATH_PROTOCOL    End;
} RAM_BLOCK_DEVICE_PATH;
#pragma pack()

STATIC RAM_BLOCK_DEVICE_PATH  mDevicePath = {
  {
    { HARDWARE_DEVICE_PATH, HW_VENDOR_DP, { (UINT8)sizeof (VENDOR_DEVICE_PATH), (UINT8)(sizeof (VENDOR_DEVICE_PATH) >> 8) } },
    { 0xA9C6827C, 0x1BD8, 0x410C, { 0x88, 0x5A, 0xC4, 0x65, 0x00, 0xF7, 0xD8, 0x36 } }
  },
  { END_DEVICE_PATH_TYPE, END_ENTIRE_DEVICE_PATH_SUBTYPE, { sizeof (EFI_DEVICE_PATH_PROTOCOL), 0 } }
};

STATIC UINT8                    *mRamBase;
STATIC EFI_BLOCK_IO_MEDIA       mMedia;

STATIC
VOID
EFIAPI
UniXNUCrashHandler (
  IN EFI_EXCEPTION_TYPE  ExceptionType,
  IN EFI_SYSTEM_CONTEXT  SystemContext
  )
{
  EFI_SYSTEM_CONTEXT_AARCH64  *AArch64Context;
  CHAR16                      Message[256];
  UINT64                      Elr;
  UINT64                      Far;
  UINT64                      Sp;
  UINT64                      Lr;

  (VOID)ExceptionType;
  AArch64Context = SystemContext.SystemContextAArch64;
  Elr            = AArch64Context->ELR;
  Far            = AArch64Context->FAR;
  Sp             = AArch64Context->SP;
  Lr             = AArch64Context->LR;

  UnicodeSPrint (
    Message,
    sizeof (Message),
    L"UNIXNURAMDISK-CRASH: elr=0x%lx rva=0x%lx far=0x%lx sp=0x%lx base=0x%lx lr=0x%lx\n",
    Elr,
    Elr - mAppBase,
    Far,
    Sp,
    mAppBase,
    Lr
    );
  if ((gST != NULL) && (gST->ConOut != NULL)) {
    gST->ConOut->OutputString (gST->ConOut, Message);
  }

  CpuDeadLoop ();
}

STATIC
EFI_STATUS
EFIAPI
RamBlockReset (
  IN EFI_BLOCK_IO_PROTOCOL  *This,
  IN BOOLEAN                ExtendedVerification
  )
{
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
RamBlockRead (
  IN  EFI_BLOCK_IO_PROTOCOL  *This,
  IN  UINT32                 MediaId,
  IN  EFI_LBA                Lba,
  IN  UINTN                  BufferSize,
  OUT VOID                   *Buffer
  )
{
  if (Buffer == NULL) {
    return EFI_INVALID_PARAMETER;
  }

  if (MediaId != mMedia.MediaId) {
    return EFI_MEDIA_CHANGED;
  }

  if ((BufferSize % RAM_BLOCK_SIZE) != 0) {
    return EFI_BAD_BUFFER_SIZE;
  }

  if ((Lba > mMedia.LastBlock) || ((BufferSize / RAM_BLOCK_SIZE) > (mMedia.LastBlock + 1 - Lba))) {
    return EFI_INVALID_PARAMETER;
  }

  CopyMem (Buffer, mRamBase + Lba * RAM_BLOCK_SIZE, BufferSize);
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
RamBlockWrite (
  IN EFI_BLOCK_IO_PROTOCOL  *This,
  IN UINT32                 MediaId,
  IN EFI_LBA                Lba,
  IN UINTN                  BufferSize,
  IN VOID                   *Buffer
  )
{
  return EFI_WRITE_PROTECTED;
}

STATIC
EFI_STATUS
EFIAPI
RamBlockFlush (
  IN EFI_BLOCK_IO_PROTOCOL  *This
  )
{
  return EFI_SUCCESS;
}

STATIC EFI_BLOCK_IO_PROTOCOL  mBlockIo = {
  EFI_BLOCK_IO_PROTOCOL_REVISION,
  &mMedia,
  RamBlockReset,
  RamBlockRead,
  RamBlockWrite,
  RamBlockFlush
};

STATIC
UINT64
FatPayloadSize (
  IN UINT64  Address
  )
{
  CONST UINT8  *Fat;
  UINT16       BytesPerSector;
  UINT32       TotalSectors;

  Fat = (CONST UINT8 *)(UINTN)Address;
  if (ReadUnaligned16 ((CONST UINT16 *)(Fat + FAT_SIGNATURE_OFFSET)) != 0xAA55) {
    return 0;
  }

  if (CompareMem (Fat + FAT_LABEL_OFFSET, "UNIXNU13", FAT_LABEL_LENGTH) != 0) {
    return 0;
  }

  BytesPerSector = ReadUnaligned16 ((CONST UINT16 *)(Fat + 0x0B));
  if ((BytesPerSector != 512) && (BytesPerSector != 1024) &&
      (BytesPerSector != 2048) && (BytesPerSector != 4096))
  {
    return 0;
  }

  TotalSectors = ReadUnaligned16 ((CONST UINT16 *)(Fat + 0x13));
  if (TotalSectors == 0) {
    TotalSectors = ReadUnaligned32 ((CONST UINT32 *)(Fat + 0x20));
  }

  if (TotalSectors == 0) {
    return 0;
  }

  return (UINT64)TotalSectors * BytesPerSector;
}

STATIC
UINT64
ProbeFatPayload (
  IN UINT64        Address,
  IN CONST CHAR8  *Source
  )
{
  CONST UINT8  *Fat;
  CHAR8        Label[FAT_LABEL_LENGTH + 1];
  UINT16       BytesPerSector;
  UINT16       TotalSectors16;
  UINT64       TotalSectors;
  INT32        SignatureMatch;
  UINTN        Index;
  UINT64       Size;

  Fat                 = (CONST UINT8 *)(UINTN)Address;
  SignatureMatch      = (ReadUnaligned16 ((CONST UINT16 *)(Fat + FAT_SIGNATURE_OFFSET)) == 0xAA55);
  BytesPerSector      = ReadUnaligned16 ((CONST UINT16 *)(Fat + 0x0B));
  TotalSectors16      = ReadUnaligned16 ((CONST UINT16 *)(Fat + 0x13));
  TotalSectors        = (TotalSectors16 != 0) ? TotalSectors16 : ReadUnaligned32 ((CONST UINT32 *)(Fat + 0x20));
  for (Index = 0; Index < FAT_LABEL_LENGTH; Index++) {
    Label[Index] = (CHAR8)Fat[FAT_LABEL_OFFSET + Index];
    if ((Label[Index] < 0x20) || (Label[Index] > 0x7E)) {
      Label[Index] = '.';
    }
  }
  Label[FAT_LABEL_LENGTH] = '\0';

  Size = FatPayloadSize (Address);
  DEBUG ((
    DEBUG_ERROR,
    "UNIXNURAMDISK: probe 0x%lx src=%a sig=%d bps=%u tot=%lu lbl=%.8a %a\n",
    Address,
    Source,
    SignatureMatch,
    BytesPerSector,
    TotalSectors,
    Label,
    (Size != 0) ? "accepted" : "rejected"
    ));

  return Size;
}

STATIC
EFI_STATUS
EFIAPI
LoadFile2Callback (
  IN     EFI_LOAD_FILE2_PROTOCOL   *This,
  IN     EFI_DEVICE_PATH_PROTOCOL  *FilePath,
  IN     BOOLEAN                   BootPolicy,
  IN OUT UINTN                     *BufferSize,
  IN     VOID                      *Buffer OPTIONAL
  )
{
  (VOID)This;
  (VOID)FilePath;
  (VOID)BootPolicy;

  if (BufferSize == NULL) {
    return EFI_INVALID_PARAMETER;
  }

  if ((Buffer == NULL) || (*BufferSize < mAppImageSize)) {
    *BufferSize = mAppImageSize;
    return EFI_BUFFER_TOO_SMALL;
  }

  CopyMem (Buffer, mAppImage, mAppImageSize);
  *BufferSize = mAppImageSize;
  return EFI_SUCCESS;
}

STATIC EFI_LOAD_FILE2_PROTOCOL  mLoadFile2 = { LoadFile2Callback };

STATIC
VOID
HoldScreen (
  IN CONST CHAR8  *Tag
  )
{
  DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK-FROZEN: %a\n", Tag));
  CpuDeadLoop ();
  return;
}

STATIC
VOID
InstallAppLoadFile2 (
  VOID
  )
{
  EFI_STATUS                Status;
  UINT16                    *BootOrder;
  UINTN                     BootOrderSize;
  CHAR16                    BootOptionName[sizeof ("Boot0000")];
  VOID                      *BootOption;
  UINTN                     BootOptionSize;
  UINT8                     *Option;
  UINT8                     *OptionEnd;
  UINT32                    Attributes;
  UINT16                    FilePathListLength;
  EFI_DEVICE_PATH_PROTOCOL  *FilePathList;
  UINT8                     *FilePathListEnd;
  EFI_DEVICE_PATH_PROTOCOL  *DevicePathNode;
  UINT16                    DevicePathNodeLength;
  MEDIA_FW_VOL_FILEPATH_DEVICE_PATH  *FvFileNode;
  EFI_GUID                  FileGuid;
  BOOLEAN                   FileGuidFound;
  EFI_HANDLE                *Handles;
  UINTN                     HandleCount;
  UINTN                     HandleIndex;
  EFI_HANDLE                FvHandle;
  EFI_FIRMWARE_VOLUME2_PROTOCOL  *Fv;
  VOID                      *ImageData;
  UINTN                     ImageSize;
  UINT32                    AuthStatus;

  BootOrder     = NULL;
  BootOrderSize = 0;
  Status        = GetEfiGlobalVariable2 (L"BootOrder", (VOID **)&BootOrder, &BootOrderSize);
  DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: bootorder %r\n", Status));
  if (EFI_ERROR (Status) || (BootOrder == NULL) || (BootOrderSize < sizeof (UINT16))) {
    if (BootOrder != NULL) {
      FreePool (BootOrder);
    }

    HoldScreen ("no BootOrder");
    return;
  }

  UnicodeSPrint (
    BootOptionName,
    sizeof (BootOptionName),
    L"Boot%04x",
    ReadUnaligned16 (BootOrder)
    );
  FreePool (BootOrder);

  BootOption     = NULL;
  BootOptionSize = 0;
  Status         = GetEfiGlobalVariable2 (BootOptionName, &BootOption, &BootOptionSize);
  DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: boot option %s %r\n", BootOptionName, Status));
  if (EFI_ERROR (Status) || (BootOption == NULL)) {
    if (BootOption != NULL) {
      FreePool (BootOption);
    }

    HoldScreen ("no boot option");
    return;
  }

  Option    = (UINT8 *)BootOption;
  OptionEnd = Option + BootOptionSize;
  if (BootOptionSize < sizeof (EFI_LOAD_OPTION)) {
    goto InvalidOption;
  }

  Attributes         = ReadUnaligned32 ((UINT32 *)Option);
  FilePathListLength = ReadUnaligned16 ((UINT16 *)(Option + sizeof (UINT32)));
  (VOID)Attributes;

  Option += sizeof (EFI_LOAD_OPTION);
  while (((UINTN)(OptionEnd - Option) >= sizeof (CHAR16)) &&
         (ReadUnaligned16 ((UINT16 *)Option) != 0))
  {
    Option += sizeof (CHAR16);
  }

  if ((UINTN)(OptionEnd - Option) < sizeof (CHAR16)) {
    goto InvalidOption;
  }

  Option += sizeof (CHAR16);
  FilePathList = (EFI_DEVICE_PATH_PROTOCOL *)Option;
  if ((FilePathListLength == 0) ||
      ((UINTN)FilePathListLength > (UINTN)(OptionEnd - Option)))
  {
    goto InvalidOption;
  }

  FileGuidFound  = FALSE;
  FilePathListEnd = (UINT8 *)FilePathList + FilePathListLength;
  DevicePathNode = FilePathList;
  while ((UINTN)(FilePathListEnd - (UINT8 *)DevicePathNode) >= sizeof (EFI_DEVICE_PATH_PROTOCOL)) {
    DevicePathNodeLength = ReadUnaligned16 ((UINT16 *)DevicePathNode->Length);
    if ((DevicePathNodeLength < sizeof (EFI_DEVICE_PATH_PROTOCOL)) ||
        (DevicePathNodeLength > (UINTN)(FilePathListEnd - (UINT8 *)DevicePathNode)))
    {
      goto InvalidOption;
    }

    if ((DevicePathNode->Type == MEDIA_DEVICE_PATH) &&
        (DevicePathNode->SubType == MEDIA_PIWG_FW_FILE_DP) &&
        (DevicePathNodeLength == sizeof (MEDIA_FW_VOL_FILEPATH_DEVICE_PATH)))
    {
      FvFileNode = (MEDIA_FW_VOL_FILEPATH_DEVICE_PATH *)DevicePathNode;
      CopyMem (&FileGuid, &FvFileNode->FvFileName, sizeof (FileGuid));
      FileGuidFound = TRUE;
    }

    if ((DevicePathNode->Type == END_DEVICE_PATH_TYPE) &&
        (DevicePathNode->SubType == END_ENTIRE_DEVICE_PATH_SUBTYPE))
    {
      break;
    }

    DevicePathNode = (EFI_DEVICE_PATH_PROTOCOL *)((UINT8 *)DevicePathNode + DevicePathNodeLength);
  }

  if (!FileGuidFound) {
    FreePool (BootOption);
    HoldScreen ("no fv file node");
    return;
  }

  Handles     = NULL;
  HandleCount = 0;
  FvHandle    = NULL;
  ImageData   = NULL;
  ImageSize   = 0;
  Status      = gBS->LocateHandleBuffer (
                       ByProtocol,
                       &gEfiFirmwareVolume2ProtocolGuid,
                       NULL,
                       &HandleCount,
                       &Handles
                       );
  if (EFI_ERROR (Status)) {
    FreePool (BootOption);
    HoldScreen ("fv read failed");
    return;
  }

  for (HandleIndex = 0; HandleIndex < HandleCount; HandleIndex++) {
    Status = gBS->HandleProtocol (
                    Handles[HandleIndex],
                    &gEfiFirmwareVolume2ProtocolGuid,
                    (VOID **)&Fv
                    );
    if (EFI_ERROR (Status)) {
      continue;
    }

    ImageData = NULL;
    ImageSize = 0;
    Status    = Fv->ReadSection (Fv, &FileGuid, EFI_SECTION_PE32, 0, &ImageData, &ImageSize, &AuthStatus);
    if (!EFI_ERROR (Status)) {
      FvHandle = Handles[HandleIndex];
      break;
    }
  }

  if (Handles != NULL) {
    FreePool (Handles);
  }

  if (FvHandle == NULL) {
    FreePool (BootOption);
    HoldScreen ("fv read failed");
    return;
  }

  if ((ImageData == NULL) || (ImageSize < 2)) {
    FreePool (BootOption);
    HoldScreen ("fv image not pe");
    return;
  }

  DEBUG ((
    DEBUG_ERROR,
    "UNIXNURAMDISK: fv image 0x%lx bytes sig=%02x %02x\n",
    ImageSize,
    ((UINT8 *)ImageData)[0],
    ((UINT8 *)ImageData)[1]
    ));
  if ((((UINT8 *)ImageData)[0] != 'M') || (((UINT8 *)ImageData)[1] != 'Z')) {
    FreePool (BootOption);
    HoldScreen ("fv image not pe");
    return;
  }

  // Keep the ReadSection buffer alive for BDS; it owns no loader copy.
  mAppImage     = (UINT8 *)ImageData;
  mAppImageSize = ImageSize;
  Status        = gBS->InstallMultipleProtocolInterfaces (
                         &FvHandle,
                         &gEfiLoadFile2ProtocolGuid,
                         &mLoadFile2,
                         NULL
                         );
  DEBUG ((
    DEBUG_ERROR,
    "UNIXNURAMDISK: loadfile2 on fv 0x%lx %r\n",
    (UINT64)(UINTN)FvHandle,
    Status
    ));
  FreePool (BootOption);
  if (EFI_ERROR (Status)) {
    HoldScreen ("loadfile2 install failed");
  }

  return;

InvalidOption:
  FreePool (BootOption);
  HoldScreen ("invalid boot option");
  return;
}

STATIC
VOID
EFIAPI
LaunchOnBds (
  IN EFI_EVENT  Event,
  IN VOID       *Context
  )
{
  EFI_STATUS                 Status;
  EFI_HANDLE                 AppImage;
  EFI_LOADED_IMAGE_PROTOCOL  *Loaded;
  EFI_CPU_ARCH_PROTOCOL      *Cpu;

  (VOID)Context;

  if (mLaunched) {
    return;
  }

  mLaunched = TRUE;
  if (Event != NULL) {
    gBS->CloseEvent (Event);
  }

  Cpu    = NULL;
  Status = gBS->LocateProtocol (&gEfiCpuArchProtocolGuid, NULL, (VOID **)&Cpu);
  if (EFI_ERROR (Status)) {
    Print (L"UNIXNURAMDISK: cpu protocol %r\n", Status);
  } else {
    Status = Cpu->RegisterInterruptHandler (
                     Cpu,
                     EXCEPT_AARCH64_SYNCHRONOUS_EXCEPTIONS,
                     UniXNUCrashHandler
                     );
    if (EFI_ERROR (Status)) {
      Print (L"UNIXNURAMDISK: cpu protocol %r\n", Status);
    }
  }

  if (gST->ConOut != NULL) {
    gST->ConOut->ClearScreen (gST->ConOut);
  }

  AppImage = NULL;
  Status   = gBS->LoadImage (TRUE, gImageHandle, NULL, mAppImage, mAppImageSize, &AppImage);
  Print (L"UNIXNURAMDISK: load2 %r\n", Status);
  if (!EFI_ERROR (Status)) {
    if (gBS->HandleProtocol (AppImage, &gEfiLoadedImageProtocolGuid, (VOID **)&Loaded) == EFI_SUCCESS) {
      Print (L"UNIXNURAMDISK: app base=0x%lx size=0x%lx\n", (UINT64)(UINTN)Loaded->ImageBase, (UINT64)Loaded->ImageSize);
      mAppBase = (UINT64)(UINTN)Loaded->ImageBase;
    }

    Status = gBS->StartImage (AppImage, NULL, NULL);
    Print (L"UNIXNURAMDISK: app start %r\n", Status);
  }

  HoldScreen ("app returned");
}

STATIC
EFI_STATUS
PublishInitrd (
  IN CONST VOID  *Fdt
  )
{
  EFI_STATUS            Status;
  INT32                 Chosen;
  CONST VOID            *StartProp;
  CONST VOID            *EndProp;
  INT32                 StartLength;
  INT32                 EndLength;
  UINT64                Start;
  UINT64                End;
  UINT64                ChosenStart;
  UINT64                ChosenEnd;
  UINT64                Size;
  UINT64                FdBase;
  UINT64                FdSize;
  UINT64                Candidate;
  UINT64                ShimHint;
  UINT64                RegionSize;
  UINTN                 MemoryMapSize;
  UINTN                 MapKey;
  UINTN                 DescriptorSize;
  UINTN                 DescriptorOffset;
  UINT32                DescriptorVersion;
  UINT32                DescriptorCount;
  EFI_MEMORY_DESCRIPTOR  *MemoryMap;
  EFI_MEMORY_DESCRIPTOR  *Descriptor;
  EFI_HANDLE  Handle;
  VOID        *Existing;
  EFI_EVENT   Event;
  EFI_EVENT   Timer;
  VOID        *Cookie;
  BOOLEAN     FromChosen;
  BOOLEAN     FromCandidate;

  if (gST->ConOut != NULL) {
    gST->ConOut->ClearScreen (gST->ConOut);
  }

  StartProp     = NULL;
  EndProp       = NULL;
  StartLength   = 0;
  EndLength     = 0;
  Start         = 0;
  End           = 0;
  ChosenStart   = 0;
  ChosenEnd     = 0;
  Size          = 0;
  MemoryMap     = NULL;
  FromChosen    = FALSE;
  FromCandidate = FALSE;

  Chosen = FdtPathOffset (Fdt, "/chosen");
  if (Chosen >= 0) {
    StartProp = FdtGetProp (Fdt, Chosen, "linux,initrd-start", &StartLength);
    EndProp   = FdtGetProp (Fdt, Chosen, "linux,initrd-end", &EndLength);
    if ((StartProp != NULL) && (EndProp != NULL) &&
        ((StartLength == 4) || (StartLength == 8)) && ((EndLength == 4) || (EndLength == 8)))
    {
      ChosenStart = ReadCells (StartProp, StartLength / 4);
      ChosenEnd   = ReadCells (EndProp, EndLength / 4);
      Start       = ChosenStart;
      End         = ChosenEnd;
    }
  }

  if ((StartProp == NULL) || (EndProp == NULL) ||
      ((StartLength != 4) && (StartLength != 8)) ||
      ((EndLength != 4) && (EndLength != 8)))
  {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: chosen 0x%lx rejected, missing or invalid initrd properties\n", Start));
  } else if ((End <= Start) || (((End - Start) % RAM_BLOCK_SIZE) != 0)) {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: chosen 0x%lx-0x%lx rejected, invalid initrd range\n", Start, End));
  } else {
    Size = ProbeFatPayload (Start, "chosen");
    if (Size != 0) {
      Size       = End - Start;
      FromChosen = TRUE;
    }
  }

  FdBase = FixedPcdGet64 (PcdFdBaseAddress);
  FdSize = FixedPcdGet32 (PcdFdSize);

  ShimHint = ReadUnaligned64 ((CONST UINT64 *)(UINTN)(FdBase + FdSize - 64));
  DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: shim hint 0x%lx\n", ShimHint));
  if (!FromChosen && (ShimHint >= 0x80000000ULL) &&
      (ShimHint < 0x100000000000ULL) && ((ShimHint & 0xF) == 0))
  {
    Candidate = ShimHint + FdSize;
    Size      = ProbeFatPayload (Candidate, "hint+FdSize");
    if (Size != 0) {
      Start         = Candidate;
      FromCandidate = TRUE;
    }
  }

  if (!FromChosen && !FromCandidate) {
    Candidate = FdBase + FdSize + 0x200;
    Size      = ProbeFatPayload (Candidate, "cand-200");
    if (Size != 0) {
      Start         = Candidate;
      FromCandidate = TRUE;
    }
  }

  if (!FromChosen && !FromCandidate) {
    Candidate = FdBase + FdSize + 0xC0;
    Size      = ProbeFatPayload (Candidate, "cand-c0");
    if (Size != 0) {
      Start         = Candidate;
      FromCandidate = TRUE;
    }
  }

  if (!FromChosen && !FromCandidate) {
    Candidate = FdBase + FdSize + 0xD0;
    Size      = ProbeFatPayload (Candidate, "cand-d0");
    if (Size != 0) {
      Start         = Candidate;
      FromCandidate = TRUE;
    }
  }

  if (!FromChosen && !FromCandidate) {
    // Report only large mapped regions; some DRAM is not safely readable with the current mappings.
    MemoryMapSize = 0;
    Status = gBS->GetMemoryMap (
                    &MemoryMapSize,
                    NULL,
                    &MapKey,
                    &DescriptorSize,
                    &DescriptorVersion
                    );
    if (Status != EFI_BUFFER_TOO_SMALL) {
      DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: memory map size query failed %r\n", Status));
    } else {
      MemoryMapSize += DescriptorSize;
      MemoryMap      = AllocatePool (MemoryMapSize);
      if (MemoryMap == NULL) {
        DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: memory map allocation failed, %lu bytes\n", MemoryMapSize));
      } else {
        Status = gBS->GetMemoryMap (
                        &MemoryMapSize,
                        MemoryMap,
                        &MapKey,
                        &DescriptorSize,
                        &DescriptorVersion
                        );
        if (Status == EFI_BUFFER_TOO_SMALL) {
          FreePool (MemoryMap);
          MemoryMapSize += DescriptorSize;
          MemoryMap      = AllocatePool (MemoryMapSize);
          if (MemoryMap == NULL) {
            DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: memory map regrowth failed, %lu bytes\n", MemoryMapSize));
          } else {
            Status = gBS->GetMemoryMap (
                            &MemoryMapSize,
                            MemoryMap,
                            &MapKey,
                            &DescriptorSize,
                            &DescriptorVersion
                            );
          }
        }

        if (MemoryMap != NULL) {
          if (EFI_ERROR (Status)) {
            DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: memory map query failed %r\n", Status));
          } else if (DescriptorSize == 0) {
            DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: memory map descriptor size is zero\n"));
          } else {
            DescriptorCount = 0;
            for (DescriptorOffset = 0;
                 DescriptorOffset + DescriptorSize <= MemoryMapSize;
                 DescriptorOffset += DescriptorSize)
            {
              Descriptor = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)MemoryMap + DescriptorOffset);
              DescriptorCount++;
              RegionSize = Descriptor->NumberOfPages * 4096ULL;
              if (RegionSize >= 0x4000000ULL) {
                DEBUG ((
                  DEBUG_ERROR,
                  "UNIXNURAMDISK: bigmap[%u] type=%u start=0x%lx size=0x%lx\n",
                  DescriptorCount - 1,
                  (UINT32)Descriptor->Type,
                  Descriptor->PhysicalStart,
                  RegionSize
                  ));
              }
            }
          }
        }
      }
    }
  }

  if (MemoryMap != NULL) {
    FreePool (MemoryMap);
    MemoryMap = NULL;
  }

  if (!FromChosen && !FromCandidate) {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: no payload found in /chosen, fixed candidates or scan\n"));
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK-FROZEN: no FAT payload; fd=0x%lx+0x%lx chosen=0x%lx-0x%lx\n", FdBase, FdSize, ChosenStart, ChosenEnd));
    CpuDeadLoop ();
    return EFI_NOT_FOUND;
  }

  if (FromChosen) {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: payload from chosen size 0x%lx\n", Size));
  } else if (FromCandidate) {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: payload from candidate 0x%lx size 0x%lx\n", Start, Size));
  }

  mRamBase                = (UINT8 *)(UINTN)Start;
  mMedia.MediaId          = 1;
  mMedia.RemovableMedia   = FALSE;
  mMedia.MediaPresent     = TRUE;
  mMedia.LogicalPartition = FALSE;
  mMedia.ReadOnly         = TRUE;
  mMedia.WriteCaching     = FALSE;
  mMedia.BlockSize        = RAM_BLOCK_SIZE;
  mMedia.IoAlign          = 1;
  mMedia.LastBlock        = Size / RAM_BLOCK_SIZE - 1;

  Handle = NULL;
  Status = gBS->InstallMultipleProtocolInterfaces (
                  &Handle,
                  &gEfiDevicePathProtocolGuid,
                  &mDevicePath,
                  &gEfiBlockIoProtocolGuid,
                  &mBlockIo,
                  NULL
                  );
  DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: initrd block device 0x%lx size 0x%lx %r\n", Start, Size, Status));
  DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK-VERDICT: FOUND start=0x%lx size=0x%lx\n", Start, Size));

  if (!EFI_ERROR (Status)) {
    EFI_SIMPLE_FILE_SYSTEM_PROTOCOL  *Fs;
    EFI_FILE_PROTOCOL                *Root;
    EFI_FILE_PROTOCOL                *File;
    EFI_FILE_INFO                    *FileInfo;
    EFI_STATUS                       FsStatus;
    EFI_STATUS                       FileStatus;
    EFI_STATUS                       ProbeStatus;
    EFI_STATUS                       LastFileStatus;
    EFI_HANDLE                       *Handles;
    UINTN                            Count;
    UINTN                            Index;
    UINTN                            FileInfoSize;
    UINT64                           FileSize;
    BOOLEAN                          FileOpened;
    BOOLEAN                          FsFound;

    Count          = 0;
    Handles        = NULL;
    ProbeStatus    = EFI_SUCCESS;
    LastFileStatus = EFI_NOT_FOUND;
    FsFound        = FALSE;
    do {
      ProbeStatus = gBS->ConnectController (Handle, NULL, NULL, TRUE);
      if (EFI_ERROR (ProbeStatus)) {
        LastFileStatus = ProbeStatus;
        break;
      }

      ProbeStatus = gBS->LocateHandleBuffer (
                           ByProtocol,
                           &gEfiSimpleFileSystemProtocolGuid,
                           NULL,
                           &Count,
                           &Handles
                           );
      if (EFI_ERROR (ProbeStatus)) {
        LastFileStatus = ProbeStatus;
        break;
      }

      for (Index = 0; (Index < Count) && (Index < 8); Index++) {
        Fs         = NULL;
        Root       = NULL;
        File       = NULL;
        FileSize   = 0;
        FileOpened = FALSE;
        FileStatus = EFI_NOT_FOUND;
        FsStatus   = gBS->HandleProtocol (
                            Handles[Index],
                            &gEfiSimpleFileSystemProtocolGuid,
                            (VOID **)&Fs
                            );
        if (!EFI_ERROR (FsStatus)) {
          FsStatus = Fs->OpenVolume (Fs, &Root);
        }

        if (!EFI_ERROR (FsStatus)) {
          FileStatus = Root->Open (
                                Root,
                                &File,
                                L"\\EFI\\UniXNU\\unixnu.toml",
                                EFI_FILE_MODE_READ,
                                0
                                );
          if (!EFI_ERROR (FileStatus)) {
            FileOpened = TRUE;
            FileInfoSize = 0;
            FileStatus   = File->GetInfo (File, &mFileInfoGuid, &FileInfoSize, NULL);
            if (FileStatus == EFI_BUFFER_TOO_SMALL) {
              FileInfo = AllocatePool (FileInfoSize);
              if (FileInfo != NULL) {
                FileStatus = File->GetInfo (File, &mFileInfoGuid, &FileInfoSize, FileInfo);
                if (!EFI_ERROR (FileStatus)) {
                  FileSize = FileInfo->FileSize;
                }

                FreePool (FileInfo);
              }
            }
          }
        } else {
          FileStatus = FsStatus;
        }

        if (File != NULL) {
          File->Close (File);
        }

        if (Root != NULL) {
          Root->Close (Root);
        }

        if (FileOpened) {
          DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: fs[%u] ok size=0x%lx\n", (UINT32)Index, FileSize));
          FsFound = TRUE;
        } else {
          LastFileStatus = FileStatus;
        }
      }
    } while (FALSE);

    if (Handles != NULL) {
      FreePool (Handles);
    }

    if (!FsFound) {
      DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: fs none (last %r)\n", LastFileStatus));
    }
  }

  InstallAppLoadFile2 ();

  Existing = NULL;
  Status   = gBS->LocateProtocol (&gUniXNULoaderProtocolGuid, NULL, &Existing);
  if (!EFI_ERROR (Status)) {
    LaunchOnBds (NULL, NULL);
    return EFI_SUCCESS;
  }

  Event  = NULL;
  Timer  = NULL;
  Cookie = NULL;
  Status = gBS->CreateEvent (
                  EVT_NOTIFY_SIGNAL,
                  TPL_CALLBACK,
                  LaunchOnBds,
                  NULL,
                  &Event
                  );
  if (!EFI_ERROR (Status)) {
    Status = gBS->RegisterProtocolNotify (
                    &gUniXNULoaderProtocolGuid,
                    Event,
                    &Cookie
                    );
  }

  if (EFI_ERROR (Status)) {
    if (Event != NULL) {
      gBS->CloseEvent (Event);
    }

    Print (L"UNIXNURAMDISK: loader notify failed %r\n", Status);
  }

  Print (L"UNIXNURAMDISK: waiting for loader %r\n", Status);

  Status = gBS->CreateEvent (
                  EVT_TIMER,
                  TPL_CALLBACK,
                  LaunchOnBds,
                  NULL,
                  &Timer
                  );
  if (!EFI_ERROR (Status)) {
    Status = gBS->SetTimer (
                    Timer,
                    TimerRelative,
                    EFI_TIMER_PERIOD_SECONDS (5)
                    );
  }

  if (EFI_ERROR (Status)) {
    if (Timer != NULL) {
      gBS->CloseEvent (Timer);
    }
  }

  Print (L"UNIXNURAMDISK: fallback timer %r\n", Status);
  return EFI_SUCCESS;
}

EFI_STATUS
EFIAPI
UniXNURamDiskDxeEntry (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_HOB_GUID_TYPE  *Hob;
  UINT64             Dtb;

  Hob = GetFirstGuidHob (&mEntryArgHobGuid);
  if (Hob == NULL) {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: no entry argument HOB\n"));
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK-FROZEN: no entry argument HOB\n"));
    CpuDeadLoop ();
    return EFI_SUCCESS;
  }

  if (GET_GUID_HOB_DATA_SIZE (Hob) < sizeof (UINT64)) {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK-FROZEN: entry argument HOB too small\n"));
    CpuDeadLoop ();
    return EFI_COMPROMISED_DATA;
  }

  Dtb = ReadUnaligned64 ((CONST UINT64 *)GET_GUID_HOB_DATA (Hob));
  if ((Dtb == 0) || ((Dtb & 3) != 0) || (FdtCheckHeader ((VOID *)(UINTN)Dtb) != 0)) {
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: x0 0x%lx is not a device tree\n", Dtb));
    DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK-FROZEN: invalid device tree 0x%lx\n", Dtb));
    CpuDeadLoop ();
    return EFI_SUCCESS;
  }

  DEBUG ((DEBUG_ERROR, "UNIXNURAMDISK: device tree at 0x%lx, %u bytes\n", Dtb, (UINT32)FdtTotalSize ((VOID *)(UINTN)Dtb)));

  return PublishInitrd ((VOID *)(UINTN)Dtb);
}
