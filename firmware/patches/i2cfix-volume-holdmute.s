; PodMic 1.1.9 add-on "i2cfix-volume-holdmute" (i2cfix + boot volume step 24 + hold-to-mute + jingle) (2026-11-14). Built from fix1/fix2 sources with all diagnostics removed.
; I2C safeguard (10 ms since 2026-11-14): wait_done replaces 'bl 0x08008f18' at the 3 blocking done-wait loops, start_hook replaces
;  'bl 0x08008f24' at the 3 blocking starts. 10 ms (ms tick 0x20001a9c, stock SysTick) -> i2c_recover: I2C1 SRESET,
;  restore FREQ/CKCFG/RT, I2CEN, clear pending EV/ER (NVIC ICPR, IRQ24/IRQ34), state = 2 -> stock retry.
; RAM 0x20001aec start-wait t0, 0x20001af0 armed flag, TS 0x20001af4 (20 B), BT 0x20001b08. Zeroed by the stock startup (.bss end literal 0x08007d78
;  patched 0x20001aa8 -> 0x20001b10), so no reset hook is needed.
.org 0x0800f460
wait_done:
  push {r4, r5, lr}
  movw r4, 0x00dc
  movt r4, 0x2000
  movw r5, 0x1a9c
  movt r5, 0x2000
  ldr r3, [r5]
wd_loop:
  ldrb r0, [r4]
  cmp r0, 0
  bne wd_ret
  ldr r2, [r5]
  subs r2, r2, r3
  cmp r2, 10
  bls wd_loop
  bl i2c_recover
  movs r0, 2
wd_ret:
  movw r1, 0x1af0
  movt r1, 0x2000
  movs r2, 0
  str r2, [r1]
  pop {r4, r5, pc}
start_hook:
  push {r0, r1, r2, r3, lr}
  movw r0, 0x1af0
  movt r0, 0x2000
  ldr r1, [r0]
  movw r2, 0x1a9c
  movt r2, 0x2000
  ldr r2, [r2]
  cmp r1, 0
  bne sh_armed
  movs r1, 1
  str r1, [r0]
  str r2, [r0, -4]
  b sh_go
sh_armed:
  ldr r3, [r0, -4]
  subs r3, r2, r3
  cmp r3, 10
  bls sh_go
  movs r1, 0
  str r1, [r0]
  bl i2c_recover
sh_go:
  pop {r0, r1, r2, r3, lr}
  b.w 0x08008f24
i2c_recover:
  push {r5, r6, lr}
  movw r5, 0x5800
  movt r5, 0x4000
  movw r3, 0x00dc
  movt r3, 0x2000
  CPSID_I
  ldr r6, [r5, 4]
  ldr r1, [r5, 0x1c]
  ldr r2, [r5, 0x20]
  and r6, r6, 0x7f
  mov.w r0, 0x8000
  str r0, [r5]
  movs r0, 0
  str r0, [r5]
  str r6, [r5, 4]
  str r1, [r5, 0x1c]
  str r2, [r5, 0x20]
  movs r0, 1
  str r0, [r5]
  movw r0, 0xe280
  movt r0, 0xe000
  mov.w r1, 0x1000000
  str r1, [r0]
  movs r1, 4
  str r1, [r0, 4]
  movs r0, 2
  strb r0, [r3]
  CPSIE_I
  pop {r5, r6, pc}
; ---------------- fix2 (2026-11-12): hold-to-mute + mute/unmute jingle ----------------
; RAM (zeroed by reset_stub, 0x20001ad0..0x20001b10):
;  TS 0x20001af4: +0 u8 last mute+1 (0 = not seen yet), +2 u8 seq id (0 idle, 1 unmute, 2 mute), +3 u8 step,
;                 +4 n samples left in segment, +8 y1, +12 y2, +16 c (2cos(w), Q30)
;  BT 0x20001b08: +0 u8 hold ticks (0 idle, 1..174 pressed, 255 long press fired)
; Tables (build script, reserved/zero vector slots 0x08004104..0x0800414b):
;  0x08004104 3 segments x {n, y1, c, y2}: 0 = 1000 Hz, 1 = 667 Hz, 2 = gap (y1 = -A sin w, y2 = -A sin 2w -> output
;  A sin(m w) from m = 0: every tone starts and ends on a zero crossing);  0x08004134 seq unmute {1,2,0,3}, mute {0,2,1,3}
; tone_hook replaces 'ldr r3,=0x20001648; ldrb r2,[r3]' at 0x0800a0aa: DSP block (DMA ISR), after the monitor mix and
;  before the wheel gain. Free regs there: r0, r1, r12, lr (pushed); r2/r3 = the replaced outputs. r5 = 32-word stereo out.
;  After a sequence ends, id/step stay on the end marker (lookup every block, harmless).
;  Any change of the mute byte 0x200001c5 (button, Unify/Connect, HID) starts the matching jingle.
tone_hook:
  push {r4, r6, r7, lr}
  movw r4, 0x1af4
  movt r4, 0x2000
  movw r0, 0x01c5
  movt r0, 0x2000
  ldrb r0, [r0]
  adds r0, 1
  ldrb r1, [r4]
  cmp r0, r1
  beq th_run
  strb r0, [r4]
  cbz r1, th_run
  strh r0, [r4, 2]
th_run:
  ldrb r0, [r4, 2]
  cbz r0, th_out
  ldr r1, [r4, 4]
  cbnz r1, th_gen
  ldrb r1, [r4, 3]
  subs r0, 1
  add.w r0, r1, r0, lsl 2
  movw r2, 0x4134
  movt r2, 0x0800
  ldrb r0, [r2, r0]
  cmp r0, 3
  beq th_out
  adds r1, 1
  strb r1, [r4, 3]
  subs r2, 48
  add.w r2, r2, r0, lsl 4
  ldm r2, {r0, r1, r2, r3}
  str r0, [r4, 4]
  str r1, [r4, 8]
  str r2, [r4, 16]
  str r3, [r4, 12]
  b th_run
th_gen:
  ldr r6, [r4, 8]
  ldr r7, [r4, 12]
  ldr.w r12, [r4, 16]
  mov r3, r5
th_loop:
  smull r0, r2, r12, r6
  lsls r2, r2, 2
  orr.w r2, r2, r0, lsr 30
  subs r2, r2, r7
  mov r7, r6
  mov r6, r2
  ldrd r0, lr, [r3]
  qadd r0, r0, r2
  qadd lr, lr, r2
  strd r0, lr, [r3], 8
  subs r1, 1
  beq th_st
  subs r0, r3, r5
  cmp r0, 128
  bne th_loop
th_st:
  str r1, [r4, 4]
  str r6, [r4, 8]
  str r7, [r4, 12]
th_out:
  subw r3, r4, 0x4ac
  ldrb r2, [r3]
  pop {r4, r6, r7, pc}
; btn_task replaces the 4 ms button consumer 0x08009298 (scheduler callback literal 0x080092f4).
;  stock press edge 0x20000d7d -> ticks = 1; while held count up; at 175 ticks (0.7 s) toggle mute (stock 0x08008de8,
;  same call the stock click uses with Unify/Connect), ticks = 255 until release; release before 0.7 s -> stock click
;  action at 0x080092a2 (monitoring toggle, or mute with Unify/Connect). Level 0x20000d7c: 0 = pressed.
btn_task:
  push {r3, lr}
  movw r2, 0x0d7d
  movt r2, 0x2000
  addw r3, r2, 0xd8b
  ldrb r1, [r3]
  ldrb r0, [r2]
  cbz r0, bt_chk
  movs r1, 0
  strb r1, [r2]
  movs r1, 1
bt_chk:
  cbz r1, bt_ret
  ldrb r0, [r2, -1]
  cbnz r0, bt_rel
  cmp r1, 255
  beq bt_ret
  adds r1, 1
  cmp r1, 175
  bne bt_st
  movs r1, 255
  strb r1, [r3]
  subw r0, r2, 0xbb8
  ldrb r0, [r0]
  movs r1, 1
  eors r0, r1
  bl 0x08008de8
  pop {r3, pc}
bt_rel:
  movs r0, 0
  strb r0, [r3]
  cmp r1, 255
  beq bt_ret
  b.w 0x080092a2
bt_st:
  strb r1, [r3]
bt_ret:
  pop {r3, pc}

