#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run real OpenCL GPU kernels and verify buffer, local-memory and math results."""
import argparse
import ctypes as C
import json
import math
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', default='FD730', help='Required GPU name substring; no CPU fallback')
    parser.add_argument('--rounds', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 1000:
        parser.error('--rounds must be between 1 and 1000')
    lib = C.CDLL('libOpenCL.so.1')
    P, I, U, Z, B = C.c_void_p, C.c_int, C.c_uint, C.c_size_t, C.c_ulonglong

    def bind(name, restype, *types):
        f = getattr(lib, name)
        f.restype, f.argtypes = restype, types
        return f

    bind('clGetPlatformIDs', I, U, C.POINTER(P), C.POINTER(U))
    bind('clGetDeviceIDs', I, P, B, U, C.POINTER(P), C.POINTER(U))
    bind('clGetDeviceInfo', I, P, U, Z, P, C.POINTER(Z))
    bind('clCreateContext', P, P, U, C.POINTER(P), P, P, C.POINTER(I))
    bind('clCreateCommandQueue', P, P, P, B, C.POINTER(I))
    bind('clCreateProgramWithSource', P, P, U, C.POINTER(C.c_char_p), C.POINTER(Z), C.POINTER(I))
    bind('clBuildProgram', I, P, U, C.POINTER(P), C.c_char_p, P, P)
    bind('clGetProgramBuildInfo', I, P, P, U, Z, P, C.POINTER(Z))
    bind('clCreateKernel', P, P, C.c_char_p, C.POINTER(I))
    bind('clCreateBuffer', P, P, B, Z, P, C.POINTER(I))
    bind('clSetKernelArg', I, P, U, Z, P)
    bind('clEnqueueNDRangeKernel', I, P, P, U, C.POINTER(Z), C.POINTER(Z), C.POINTER(Z), U, P, P)
    bind('clEnqueueReadBuffer', I, P, P, U, Z, Z, P, U, P, P)
    bind('clFinish', I, P)
    for name in ['clReleaseKernel', 'clReleaseMemObject', 'clReleaseProgram', 'clReleaseCommandQueue', 'clReleaseContext']:
        bind(name, I, P)

    def check(code):
        if code != 0:
            raise RuntimeError(f'OpenCL error {code}')

    def info(device, param):
        size = Z()
        check(lib.clGetDeviceInfo(device, param, 0, None, C.byref(size)))
        buf = C.create_string_buffer(size.value)
        check(lib.clGetDeviceInfo(device, param, size, buf, None))
        return buf.value.decode()

    count = U()
    check(lib.clGetPlatformIDs(0, None, C.byref(count)))
    platforms = (P * count.value)()
    check(lib.clGetPlatformIDs(count, platforms, None))
    selected = None
    names = []
    for platform in platforms:
        n = U()
        code = lib.clGetDeviceIDs(platform, 4, 0, None, C.byref(n))
        if code == -1:
            continue
        check(code)
        devices = (P * n.value)()
        check(lib.clGetDeviceIDs(platform, 4, n, devices, None))
        for device in devices:
            name = info(device, 0x102B)
            names.append(name)
            if args.device in name:
                selected = P(device)
                break
    if selected is None:
        raise RuntimeError(f'Required GPU {args.device!r} not found; GPU devices: {names}')

    report = {'device': info(selected, 0x102B), 'version': info(selected, 0x102F),
              'driver': info(selected, 0x102D), 'rounds': args.rounds, 'elements': 65536}
    owned = []
    error = I()

    def own(name, handle):
        check(error.value)
        if not handle:
            raise RuntimeError(f'{name}: null OpenCL handle')
        owned.append((name, handle))
        return handle

    try:
        context = own('clReleaseContext', lib.clCreateContext(None, 1, C.byref(selected), None, None, C.byref(error)))
        queue = own('clReleaseCommandQueue', lib.clCreateCommandQueue(context, selected, 0, C.byref(error)))
        source = C.c_char_p(b'''
__kernel void add(__global const float *a, __global const float *b, __global float *out) {
    size_t i=get_global_id(0); out[i]=a[i]+b[i];
}
__kernel void reduce(__global const float *a, __global uint *out, __local uint *scratch) {
    uint i=get_local_id(0); scratch[i]=(uint)a[get_global_id(0)];
    barrier(CLK_LOCAL_MEM_FENCE);
    for(uint stride=get_local_size(0)/2;stride;stride/=2) {
        if(i<stride) scratch[i]+=scratch[i+stride];
        barrier(CLK_LOCAL_MEM_FENCE);
    }
    if(i==0) out[get_group_id(0)]=scratch[0];
}
__kernel void math_probe(__global const float *a, __global float *out) {
    size_t i=get_global_id(0); out[i]=sqrt(a[i]+1.0f);
}
''')
        program = own('clReleaseProgram', lib.clCreateProgramWithSource(context, 1, C.byref(source), None, C.byref(error)))
        result = lib.clBuildProgram(program, 1, C.byref(selected), b'-cl-std=CL1.2', None, None)
        logsize = Z()
        check(lib.clGetProgramBuildInfo(program, selected, 0x1183, 0, None, C.byref(logsize)))
        log = C.create_string_buffer(max(1, logsize.value))
        check(lib.clGetProgramBuildInfo(program, selected, 0x1183, len(log), log, None))
        report['build_log'] = log.value.decode()
        if result:
            raise RuntimeError(f'OpenCL compiler error {result}: {report["build_log"]}')
        kernels = {name: own('clReleaseKernel', lib.clCreateKernel(program, name.encode(), C.byref(error)))
                   for name in ['add', 'reduce', 'math_probe']}
        n = report['elements']
        a = (C.c_float * n)(*[i % 97 for i in range(n)])
        b = (C.c_float * n)(*[(i % 61) * .25 for i in range(n)])
        output = (C.c_float * n)()
        sums = (U * (n // 64))()
        buffers = [own('clReleaseMemObject', lib.clCreateBuffer(context, 4 | 32, C.sizeof(data), data, C.byref(error)))
                   for data in [a, b]]
        buffers += [own('clReleaseMemObject', lib.clCreateBuffer(context, 2, n * 4, None, C.byref(error))),
                    own('clReleaseMemObject', lib.clCreateBuffer(context, 2, C.sizeof(sums), None, C.byref(error)))]

        def arguments(kernel, items):
            for index, item in enumerate(items):
                value = P(item)
                check(lib.clSetKernelArg(kernel, index, C.sizeof(value), C.byref(value)))

        arguments(kernels['add'], buffers[:3])
        arguments(kernels['reduce'], [buffers[0], buffers[3]])
        check(lib.clSetKernelArg(kernels['reduce'], 2, 64 * 4, None))
        arguments(kernels['math_probe'], [buffers[0], buffers[2]])
        global_size, local_size = Z(n), Z(64)

        def run(name, buffer, data):
            check(lib.clEnqueueNDRangeKernel(queue, kernels[name], 1, None, C.byref(global_size), C.byref(local_size), 0, None, None))
            check(lib.clEnqueueReadBuffer(queue, buffer, 1, 0, C.sizeof(data), data, 0, None, None))

        start = time.monotonic()
        for _ in range(args.rounds):
            run('add', buffers[2], output)
            assert all(output[i] == a[i] + b[i] for i in range(n)), 'vector addition mismatch'
            run('reduce', buffers[3], sums)
            assert all(sums[i] == sum(int(a[j]) for j in range(i * 64, (i + 1) * 64))
                       for i in range(n // 64)), 'local-memory/barrier reduction mismatch'
            run('math_probe', buffers[2], output)
            assert all(abs(output[i] - math.sqrt(a[i] + 1)) < 2e-5 for i in range(n)), 'libclc sqrt mismatch'
        check(lib.clFinish(queue))
        report.update(status='PASS', seconds=time.monotonic()-start,
                      verified=['vector addition', 'local memory and barriers', 'libclc sqrt'])
    finally:
        for name, handle in reversed(owned):
            check(getattr(lib, name)(handle))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
