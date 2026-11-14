; PodMic 1.1.9 "diag" add-on. Placed in the free 0xFF area after the .data init image.
; RAM: VARS 0x20001ab0 (+4 mlcount, +8 last, +12 stall)   MIRROR 0x20001ac0 (+0 counts, +4 hang tag, +5 reset cause)
; BKP (RTC backup regs, survive IWDG/system reset): 0x40002850..0x40002860
;  BKP0 = 0xD1A6<<16 | hangs<<8 | boots      BKP1 = PC at 1 s
;  BKP2 = STAT0 | STAT1<<16                  BKP3 = CTL0 | CTL1<<16
;  BKP4 = PC@2s low16 | i2c_state<<16 | IPSR<<24
.org 0x0800f460
reset_stub:
  movw r0, 0x1ab0
  movt r0, 0x2000
  movs r1, 0
  str r1, [r0, 4]
  str r1, [r0, 8]
  str r1, [r0, 12]
  movw r0, 0x1024
  movt r0, 0x4002
  ldr r1, [r0]
  lsrs r1, r1, 24
  movw r0, 0x1ac5
  movt r0, 0x2000
  strb r1, [r0]
  bl bkp_unlock
  movw r0, 0x2850
  movt r0, 0x4000
  ldr r1, [r0]
  lsrs r2, r1, 16
  movw r3, 0xd1a6
  cmp r2, r3
  beq rs_ok
  lsls r1, r3, 16
rs_ok:
  adds r2, r1, 1
  bfi r1, r2, 0, 8
  str r1, [r0]
  b.w 0x08007d30
bkp_unlock:
  movw r0, 0x101c
  movt r0, 0x4002
  ldr r1, [r0]
  orr r1, r1, 0x10000000
  str r1, [r0]
  movw r0, 0x7000
  movt r0, 0x4000
  ldr r1, [r0]
  orr r1, r1, 0x100
  str r1, [r0]
  ldr r1, [r0]
  bx lr
ml_hook:
  push {r0, r1}
  movw r0, 0x1ab4
  movt r0, 0x2000
  ldr r1, [r0]
  adds r1, 1
  str r1, [r0]
  pop {r0, r1}
  b.w 0x08009c4c
st_hook:
  tst lr, 4
  ite eq
  MRS_R12_MSP
  MRS_R12_PSP
  movw r3, 0x1ab0
  movt r3, 0x2000
  ldr r0, [r3, 4]
  cmp r0, 0
  beq chain
  ldr r1, [r3, 8]
  cmp r0, r1
  beq stalled
  str r0, [r3, 8]
  movs r0, 0
  str r0, [r3, 12]
  b chain
stalled:
  ldr r1, [r3, 12]
  adds r1, 1
  str r1, [r3, 12]
  movw r2, 1000
  cmp r1, r2
  beq snap1
  movw r2, 2000
  cmp r1, r2
  beq snap2
chain:
  b.w 0x0800c390
snap1:
  push {r4, r5, lr}
  bl bkp_unlock
  movw r4, 0x2850
  movt r4, 0x4000
  ldr r5, [r4]
  lsrs r0, r5, 16
  movw r1, 0xd1a6
  cmp r0, r1
  beq s1
  lsls r5, r1, 16
s1:
  ubfx r0, r5, 8, 8
  adds r0, 1
  bfi r5, r0, 8, 8
  str r5, [r4]
  ldr.w r0, [r12, 24]
  str r0, [r4, 4]
  movw r1, 0x5800
  movt r1, 0x4000
  ldr r0, [r1, 0x18]
  ldr r2, [r1, 0x14]
  uxth r2, r2
  orr.w r2, r2, r0, lsl 16
  str r2, [r4, 8]
  ldr r0, [r1, 4]
  ldr r2, [r1]
  uxth r2, r2
  orr.w r2, r2, r0, lsl 16
  str r2, [r4, 12]
  movw r1, 0x00dc
  movt r1, 0x2000
  ldrb r0, [r1]
  lsls r0, r0, 16
  ldr.w r2, [r12, 28]
  uxtb r2, r2
  orr.w r0, r0, r2, lsl 24
  str r0, [r4, 16]
  ldr r0, [r1, 0x10]
  ldr r2, [r1, 0x18]
  uxth r0, r0
  orr.w r0, r0, r2, lsl 16
  movw r1, 0x1ac0
  movt r1, 0x2000
  str r0, [r1]
  ubfx r0, r5, 8, 8
  strb r0, [r1, 4]
  pop {r4, r5, lr}
  b chain
snap2:
  push {r4, r5, lr}
  bl bkp_unlock
  movw r4, 0x2850
  movt r4, 0x4000
  ldr r5, [r4, 16]
  ldr.w r0, [r12, 24]
  bfi r5, r0, 0, 16
  str r5, [r4, 16]
  pop {r4, r5, lr}
  b chain
diag_reply:
  movs r0, 3
  strb r0, [r4, 6]
  movw r1, 0x2850
  movt r1, 0x4000
  movs r3, 0
dl:
  ldr.w r0, [r1, r3]
  adds r2, r4, r3
  str.w r0, [r2, 9]
  adds r3, 4
  cmp r3, 20
  bne dl
  movw r1, 0x1ac0
  movt r1, 0x2000
  ldr r0, [r1]
  str.w r0, [r4, 29]
  ldrb r0, [r1, 4]
  strb r0, [r4, 0x21]
  ldrb r0, [r1, 5]
  strb r0, [r4, 0x22]
  b.w 0x080086b4
