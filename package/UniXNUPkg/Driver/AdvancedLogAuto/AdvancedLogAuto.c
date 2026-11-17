/** @file
  Early-boot QR renderer for the Advanced Logger log.

  The complete log is collected once and then displayed continuously as
  OSEND frames.  Frames alternate between the end and the start of the log so
  a short screen recording captures both the latest messages and boot head.
  This driver deliberately has no menu or input dependency.

  Copyright (c) 2026, UniXNU contributors. All rights reserved.
  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <Uefi.h>
#include <Protocol/AdvancedLogger.h>
#include <Protocol/GraphicsOutput.h>
#include <Library/AdvancedLoggerAccessLib.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/QrEncoderLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>

#define ONESEND_BLOCK_LENGTH   720
#define ONESEND_HEADER_LENGTH  28
#define ONESEND_CRC_LENGTH     4
#define ONESEND_FRAME_LENGTH   (ONESEND_HEADER_LENGTH + ONESEND_BLOCK_LENGTH + ONESEND_CRC_LENGTH)

#define LOGGER_WAIT_ATTEMPTS   50
#define LOGGER_WAIT_INTERVAL   100000
#define FRAME_INTERVAL         400000
#define SCREEN_MARGIN_DIVISOR  16
#define FRAME_MARKER_BITS      8
#define LOG_FILE_NAME_SIZE     32

STATIC CONST CHAR8  mLogModel[] = "UniXNU";

STATIC CONST EFI_GRAPHICS_OUTPUT_BLT_PIXEL  mBackground = { 0x0F, 0x0C, 0x0B, 0 };
STATIC CONST EFI_GRAPHICS_OUTPUT_BLT_PIXEL  mWhite      = { 0xFF, 0xFF, 0xFF, 0 };
STATIC CONST EFI_GRAPHICS_OUTPUT_BLT_PIXEL  mMarkerOff  = { 0x40, 0x40, 0x40, 0 };

STATIC
UINT32
Crc32 (
  IN CONST UINT8  *Data,
  IN UINTN        Length
  )
{
  UINT32  Crc;
  UINTN   Index;
  UINTN   Bit;

  Crc = 0xFFFFFFFFu;
  for (Index = 0; Index < Length; Index++) {
    Crc ^= Data[Index];
    for (Bit = 0; Bit < 8; Bit++) {
      Crc = (Crc >> 1) ^ ((0u - (Crc & 1u)) & 0xEDB88320u);
    }
  }

  return Crc ^ 0xFFFFFFFFu;
}

STATIC
VOID
PutLe16 (
  OUT UINT8   *Dest,
  IN  UINT16  Value
  )
{
  Dest[0] = (UINT8)Value;
  Dest[1] = (UINT8)(Value >> 8);
}

STATIC
VOID
PutLe32 (
  OUT UINT8   *Dest,
  IN  UINT32  Value
  )
{
  Dest[0] = (UINT8)Value;
  Dest[1] = (UINT8)(Value >> 8);
  Dest[2] = (UINT8)(Value >> 16);
  Dest[3] = (UINT8)(Value >> 24);
}

STATIC
VOID
AppendDecimal (
  OUT CHAR8  *Dest,
  IN  UINTN  Value,
  IN  UINTN  Digits
  )
{
  UINTN  Index;

  for (Index = Digits; Index != 0; Index--) {
    Dest[Index - 1] = (CHAR8)('0' + Value % 10);
    Value /= 10;
  }
}

STATIC
UINTN
BuildLogFileName (
  OUT CHAR8  *FileName
  )
{
  EFI_TIME  Time;
  UINTN     Offset;

  ZeroMem (&Time, sizeof (Time));
  gST->RuntimeServices->GetTime (&Time, NULL);

  CopyMem (FileName, mLogModel, sizeof (mLogModel) - 1);
  Offset = sizeof (mLogModel) - 1;
  FileName[Offset++] = '-';
  AppendDecimal (FileName + Offset, Time.Year, 4);
  Offset += 4;
  AppendDecimal (FileName + Offset, Time.Month, 2);
  Offset += 2;
  AppendDecimal (FileName + Offset, Time.Day, 2);
  Offset += 2;
  FileName[Offset++] = '-';
  AppendDecimal (FileName + Offset, Time.Hour, 2);
  Offset += 2;
  AppendDecimal (FileName + Offset, Time.Minute, 2);
  Offset += 2;
  AppendDecimal (FileName + Offset, Time.Second, 2);
  Offset += 2;
  CopyMem (FileName + Offset, ".log", 4);
  Offset += 4;
  FileName[Offset] = '\0';
  return Offset;
}

STATIC
UINT8 *
BuildEnvelope (
  IN  CONST UINT8  *Log,
  IN  UINTN        LogLen,
  OUT UINTN        *EnvelopeLen
  )
{
  STATIC CONST CHAR8  Mime[] = "text/plain";
  CHAR8               FileName[LOG_FILE_NAME_SIZE];
  UINTN               NameLen;
  UINTN               MimeLen;
  UINTN               Total;
  UINTN               DataStart;
  UINT32              Checksum;
  UINT8               *Envelope;

  NameLen = BuildLogFileName (FileName);
  MimeLen = sizeof (Mime) - 1;
  Total   = 26 + NameLen + MimeLen + LogLen;

  Envelope = AllocatePool (Total);
  if (Envelope == NULL) {
    return NULL;
  }

  Checksum = Crc32 (Log, LogLen);
  Envelope[0] = 'L';
  Envelope[1] = 'M';
  Envelope[2] = 'S';
  Envelope[3] = '1';
  Envelope[4] = 2;
  Envelope[5] = 0;
  PutLe16 (Envelope + 6, (UINT16)NameLen);
  PutLe16 (Envelope + 8, (UINT16)MimeLen);
  PutLe32 (Envelope + 10, (UINT32)LogLen);
  PutLe32 (Envelope + 14, Checksum);
  PutLe32 (Envelope + 18, (UINT32)LogLen);
  PutLe32 (Envelope + 22, Checksum);
  CopyMem (Envelope + 26, FileName, NameLen);
  DataStart = 26 + NameLen;
  CopyMem (Envelope + DataStart, Mime, MimeLen);
  DataStart += MimeLen;
  if (LogLen != 0) {
    CopyMem (Envelope + DataStart, Log, LogLen);
  }

  *EnvelopeLen = Total;
  return Envelope;
}

STATIC
VOID
BuildFrame (
  IN  CONST UINT8  *Payload,
  IN  UINTN        PayloadLen,
  IN  UINTN        BlockIndex,
  IN  UINT32       SessionId,
  IN  UINT32       PayloadChecksum,
  OUT UINT8        *Frame
  )
{
  UINTN   BlockCount;
  UINTN   Offset;
  UINTN   ChunkLen;
  UINT32  Sequence;
  UINT32  Checksum;

  BlockCount = (PayloadLen + ONESEND_BLOCK_LENGTH - 1) / ONESEND_BLOCK_LENGTH;
  Sequence   = (UINT32)(BlockIndex + BlockIndex / 4);
  ZeroMem (Frame, ONESEND_FRAME_LENGTH);
  Frame[0] = 'O';
  Frame[1] = 'S';
  Frame[2] = 2;
  Frame[3] = 0;
  PutLe32 (Frame + 4, SessionId);
  PutLe32 (Frame + 8, Sequence);
  PutLe32 (Frame + 12, (UINT32)BlockCount);
  PutLe16 (Frame + 16, ONESEND_BLOCK_LENGTH);
  PutLe16 (Frame + 18, 0);
  PutLe32 (Frame + 20, (UINT32)PayloadLen);
  PutLe32 (Frame + 24, PayloadChecksum);

  Offset   = BlockIndex * ONESEND_BLOCK_LENGTH;
  ChunkLen = MIN ((UINTN)ONESEND_BLOCK_LENGTH, PayloadLen - Offset);
  CopyMem (Frame + ONESEND_HEADER_LENGTH, Payload + Offset, ChunkLen);
  Checksum = Crc32 (Frame, ONESEND_HEADER_LENGTH + ONESEND_BLOCK_LENGTH);
  PutLe32 (Frame + ONESEND_HEADER_LENGTH + ONESEND_BLOCK_LENGTH, Checksum);
}

STATIC
CHAR8 *
CollectLog (
  OUT UINTN  *LogLen
  )
{
  ADVANCED_LOGGER_ACCESS_MESSAGE_LINE_ENTRY  Line;
  EFI_STATUS                                Status;
  CHAR8                                     *Log;
  CHAR8                                     *New;
  UINTN                                     Len;
  UINTN                                     Cap;
  UINTN                                     NewCap;

  Log = NULL;
  Len = 0;
  Cap = 0;
  ZeroMem (&Line, sizeof (Line));
  AdvancedLoggerAccessLibReset (&Line);

  for ( ; ;) {
    Status = AdvancedLoggerAccessLibGetNextFormattedLine (&Line);
    if (Status == EFI_END_OF_FILE) {
      break;
    }

    if (EFI_ERROR (Status) || (Line.MessageLen == 0)) {
      break;
    }

    if ((Len + Line.MessageLen) > Cap) {
      NewCap = MAX (Len + Line.MessageLen, Cap * 2);
      NewCap = MAX (NewCap, (UINTN)4096);
      if (Log == NULL) {
        New = AllocatePool (NewCap);
      } else {
        New = ReallocatePool (Cap, NewCap, Log);
      }

      if (New == NULL) {
        break;
      }

      Log = New;
      Cap = NewCap;
    }

    CopyMem (Log + Len, Line.Message, Line.MessageLen);
    Len += Line.MessageLen;
  }

  AdvancedLoggerAccessLibReset (&Line);
  *LogLen = Len;
  return Log;
}

STATIC
VOID
DrawFrameMarker (
  IN EFI_GRAPHICS_OUTPUT_PROTOCOL  *Gop,
  IN UINT32                        Width,
  IN UINT32                        Height,
  IN UINT32                        QrBottom,
  IN UINTN                         FrameIndex
  )
{
  UINT32                          Margin;
  UINT32                          DotSize;
  UINT32                          Gap;
  UINT32                          MarkerWidth;
  UINT32                          MarkerHeight;
  UINT32                          X;
  UINT32                          Y;
  UINTN                           Bit;
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  Color;

  Margin       = MIN (Width, Height) / SCREEN_MARGIN_DIVISOR;
  DotSize      = MAX ((UINT32)2, Margin / 6);
  Gap          = MAX ((UINT32)2, DotSize / 2);
  MarkerWidth  = FRAME_MARKER_BITS * DotSize + (FRAME_MARKER_BITS - 1) * Gap;
  MarkerHeight = DotSize;
  if (MarkerWidth > Width) {
    return;
  }

  X = (Width - MarkerWidth) / 2;
  if (Height > QrBottom + MarkerHeight) {
    Y = QrBottom + (Height - QrBottom - MarkerHeight) / 2;
  } else {
    Y = Height - MarkerHeight;
  }

  for (Bit = 0; Bit < FRAME_MARKER_BITS; Bit++) {
    if ((FrameIndex & ((UINTN)1 << (FRAME_MARKER_BITS - 1 - Bit))) != 0) {
      Color = mWhite;
    } else {
      Color = mMarkerOff;
    }

    Gop->Blt (
           Gop,
           &Color,
           EfiBltVideoFill,
           0,
           0,
           X + (UINT32)Bit * (DotSize + Gap),
           Y,
           DotSize,
           MarkerHeight,
           0
           );
  }
}

STATIC
VOID
DrawQrFrame (
  IN EFI_GRAPHICS_OUTPUT_PROTOCOL  *Gop,
  IN CONST UINT8                   *Frame,
  IN UINTN                         FrameIndex
  )
{
  EFI_STATUS                     Status;
  UINT32                         Width;
  UINT32                         Height;
  UINT32                         Margin;
  UINT32                         RegionSize;
  UINT32                         X;
  UINT32                         Y;
  EFI_GRAPHICS_OUTPUT_BLT_PIXEL  *Bitmap;

  if ((Gop->Mode == NULL) || (Gop->Mode->Info == NULL)) {
    return;
  }

  Width  = Gop->Mode->Info->HorizontalResolution;
  Height = Gop->Mode->Info->VerticalResolution;
  Margin = MIN (Width, Height) / SCREEN_MARGIN_DIVISOR;
  if ((MIN (Width, Height) <= 2 * Margin) || (Height == 0)) {
    return;
  }

  RegionSize = MIN (Width, Height) - 2 * Margin;
  X          = (Width - RegionSize) / 2;
  Y          = (Height - RegionSize) / 2;

  Gop->Blt (
         Gop,
         (EFI_GRAPHICS_OUTPUT_BLT_PIXEL *)&mBackground,
         EfiBltVideoFill,
         0,
         0,
         0,
         0,
         Width,
         Height,
         0
         );

  Bitmap = NULL;
  Status = QrEncodeData (
             QrAutoVersion,
             QrECLevel_L,
             QrByteMode,
             0,
             (UINT8 *)Frame,
             ONESEND_FRAME_LENGTH,
             (INTN)RegionSize,
             &Bitmap
             );
  if (!EFI_ERROR (Status) && (Bitmap != NULL)) {
    Gop->Blt (
           Gop,
           Bitmap,
           EfiBltBufferToVideo,
           0,
           0,
           X,
           Y,
           RegionSize,
           RegionSize,
           0
           );
  }

  if (Bitmap != NULL) {
    FreePool (Bitmap);
  }

  DrawFrameMarker (Gop, Width, Height, Y + RegionSize, FrameIndex);
}

EFI_STATUS
EFIAPI
AdvancedLogAutoEntry (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  ADVANCED_LOGGER_PROTOCOL     *LoggerProtocol;
  EFI_GRAPHICS_OUTPUT_PROTOCOL  *Gop;
  EFI_STATUS                    Status;
  CHAR8                         *Log;
  UINTN                         LogLen;
  UINT8                         *Payload;
  UINTN                         PayloadLen;
  UINTN                         BlockCount;
  UINT32                        PayloadChecksum;
  UINT32                        SessionId;
  UINTN                         WaitIndex;
  UINTN                         FrameIndex;
  UINTN                         FrameStep;
  UINTN                         BlockIndex;
  UINT8                         Frame[ONESEND_FRAME_LENGTH];

  (VOID)ImageHandle;
  (VOID)SystemTable;

  Status = EFI_NOT_FOUND;
  for (WaitIndex = 0; WaitIndex < LOGGER_WAIT_ATTEMPTS; WaitIndex++) {
    Status = gBS->LocateProtocol (
                    &gAdvancedLoggerProtocolGuid,
                    NULL,
                    (VOID **)&LoggerProtocol
                    );
    if (!EFI_ERROR (Status)) {
      break;
    }

    if (WaitIndex + 1 < LOGGER_WAIT_ATTEMPTS) {
      gBS->Stall (LOGGER_WAIT_INTERVAL);
    }
  }

  if (EFI_ERROR (Status)) {
    return Status;
  }

  Status = gBS->LocateProtocol (
                  &gEfiGraphicsOutputProtocolGuid,
                  NULL,
                  (VOID **)&Gop
                  );
  if (EFI_ERROR (Status)) {
    return Status;
  }

  if ((Gop->Mode == NULL) || (Gop->Mode->Info == NULL)) {
    return EFI_UNSUPPORTED;
  }

  Log     = CollectLog (&LogLen);
  Payload = BuildEnvelope ((CONST UINT8 *)Log, LogLen, &PayloadLen);
  if (Log != NULL) {
    FreePool (Log);
  }

  if (Payload == NULL) {
    return EFI_OUT_OF_RESOURCES;
  }

  BlockCount      = (PayloadLen + ONESEND_BLOCK_LENGTH - 1) / ONESEND_BLOCK_LENGTH;
  PayloadChecksum = Crc32 (Payload, PayloadLen);
  SessionId       = 0x9E3779B9u ^ (UINT32)(UINTN)Gop ^ (UINT32)PayloadLen;
  if (SessionId == 0) {
    SessionId = 1;
  }

  DEBUG ((
    DEBUG_WARN,
    "AdvancedLogAuto: %lu log bytes, %lu blocks, %lu x %lu\n",
    (UINTN)LogLen,
    (UINTN)BlockCount,
    (UINTN)Gop->Mode->Info->HorizontalResolution,
    (UINTN)Gop->Mode->Info->VerticalResolution
    ));

  FrameIndex = 0;
  for ( ; ;) {
    //
    // One pass visits every block exactly once.  Wrap only after the tail and
    // head cursors meet, avoiding the repeated middle block.
    //
    FrameStep = FrameIndex % BlockCount;
    if ((FrameStep & 1) == 0) {
      BlockIndex = BlockCount - 1 - FrameStep / 2;
    } else {
      BlockIndex = FrameStep / 2;
    }

    BuildFrame (
      Payload,
      PayloadLen,
      BlockIndex,
      SessionId,
      PayloadChecksum,
      Frame
      );
    DrawQrFrame (Gop, Frame, FrameIndex);

    FrameIndex++;
    gBS->Stall (FRAME_INTERVAL);
  }
}
