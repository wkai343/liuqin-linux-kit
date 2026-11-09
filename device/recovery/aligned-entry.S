/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Xiaomi ABL loads a raw ARM64 Image at a legacy unaligned offset. Reserve
 * enough room in the outer Image and move the real kernel to the next 2 MiB
 * boundary before entering it. x0 remains the bootloader's FDT argument.
 */
.section .text
.global _start
_start:
	b	relocate
	nop
	.quad	0
	.quad	0x200000 + KERNEL_IMAGE_SIZE
	.quad	KERNEL_FLAGS
	.quad	0, 0, 0
	.long	0x644d5241
	.long	0
relocate:
	adr	x10, kernel_start
	add	x11, x10, #0x200, lsl #12
	sub	x11, x11, #1
	lsr	x11, x11, #21
	lsl	x11, x11, #21
	ldr	x12, kernel_file_size
	add	x13, x10, x12
	add	x14, x11, x12
copy_backwards:
	sub	x13, x13, #16
	sub	x14, x14, #16
	ldp	x15, x16, [x13]
	stp	x15, x16, [x14]
	subs	x12, x12, #16
	b.ne	copy_backwards
	dsb	sy
	ic	iallu
	dsb	sy
	isb
	mov	x1, xzr
	mov	x2, xzr
	mov	x3, xzr
	br	x11
.balign 8
kernel_file_size:
	.quad	kernel_end - kernel_start
.balign 4096
kernel_start:
	.incbin KERNEL_FILE
.balign 16
kernel_end:
	.space	0x200000 - (kernel_start - _start)
