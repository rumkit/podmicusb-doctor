; PodMic 1.1.9 add-on "i2cfix" (I2C IRQ priority + I2C timeout/recovery) (2026-11-14). Built from fix1/fix2 sources with all diagnostics removed.
; I2C safeguard (10 ms since 2026-11-14): wait_done replaces 'bl 0x08008f18' at the 3 blocking done-wait loops, start_hook replaces
;  'bl 0x08008f24' at the 3 blocking starts. 10 ms (ms tick 0x20001a9c, stock SysTick) -> i2c_recover: I2C1 SRESET,
;  restore FREQ/CKCFG/RT, I2CEN, clear pending EV/ER (NVIC ICPR, IRQ24/IRQ34), state = 2 -> stock retry.
; RAM 0x20001aec start-wait t0, 0x20001af0 armed flag. Zeroed by the stock startup (.bss end literal 0x08007d78
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
