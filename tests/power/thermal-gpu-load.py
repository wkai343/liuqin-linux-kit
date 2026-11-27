#!/usr/bin/env python3
"""Offscreen FD730 load for thermal-device.py, with render/compute readback.

Applies GPU load for 100 seconds; the thermal test normally terminates it sooner.
It does not change frequency governors, frequency limits or thermal settings.
"""
import ctypes as C
import json
import os
from pathlib import Path
import time

os.environ['EGL_PLATFORM'] = 'surfaceless'
E = C.CDLL('libEGL.so.1')
G = C.CDLL('libGLESv2.so.2')
I, U, P, F = C.c_int, C.c_uint, C.c_void_p, C.c_float

def bind(lib, name, ret, *args):
    f = getattr(lib, name)
    f.restype, f.argtypes = ret, args
    return f

for name, ret, args in [
    ('eglGetDisplay', P, [P]), ('eglInitialize', U, [P, C.POINTER(I), C.POINTER(I)]),
    ('eglBindAPI', U, [U]), ('eglChooseConfig', U, [P, C.POINTER(I), C.POINTER(P), I, C.POINTER(I)]),
    ('eglCreatePbufferSurface', P, [P, P, C.POINTER(I)]),
    ('eglCreateContext', P, [P, P, P, C.POINTER(I)]),
    ('eglMakeCurrent', U, [P, P, P, P]), ('eglDestroyContext', U, [P, P]),
    ('eglDestroySurface', U, [P, P]), ('eglTerminate', U, [P]),
]:
    bind(E, name, ret, *args)
for name, ret, args in [
    ('glGetString', C.c_char_p, [U]), ('glGetError', U, []),
    ('glCreateShader', U, [U]), ('glShaderSource', None, [U, I, C.POINTER(C.c_char_p), C.POINTER(I)]),
    ('glCompileShader', None, [U]), ('glGetShaderiv', None, [U, U, C.POINTER(I)]),
    ('glGetShaderInfoLog', None, [U, I, C.POINTER(I), P]), ('glDeleteShader', None, [U]),
    ('glCreateProgram', U, []), ('glAttachShader', None, [U, U]),
    ('glLinkProgram', None, [U]), ('glGetProgramiv', None, [U, U, C.POINTER(I)]),
    ('glGetProgramInfoLog', None, [U, I, C.POINTER(I), P]),
    ('glUseProgram', None, [U]), ('glDeleteProgram', None, [U]),
    ('glViewport', None, [I, I, I, I]), ('glDrawArrays', None, [U, I, I]),
    ('glFinish', None, []), ('glReadPixels', None, [I, I, I, I, U, U, P]),
    ('glGenBuffers', None, [I, C.POINTER(U)]), ('glBindBuffer', None, [U, U]),
    ('glBufferData', None, [U, C.c_ssize_t, P, U]), ('glBindBufferBase', None, [U, U, U]),
    ('glDispatchCompute', None, [U, U, U]), ('glMemoryBarrier', None, [U]),
    ('glMapBufferRange', P, [U, C.c_ssize_t, C.c_ssize_t, U]),
    ('glUnmapBuffer', U, [U]), ('glDeleteBuffers', None, [I, C.POINTER(U)]),
]:
    bind(G, name, ret, *args)

def array(*v):
    return (I * len(v))(*v)

def program(parts):
    shaders = []
    p = G.glCreateProgram()
    for kind, source in parts:
        shader = G.glCreateShader(kind)
        text = C.c_char_p(source.encode())
        G.glShaderSource(shader, 1, C.byref(text), None)
        G.glCompileShader(shader)
        ok = I()
        G.glGetShaderiv(shader, 0x8B81, C.byref(ok))
        log = C.create_string_buffer(8192)
        G.glGetShaderInfoLog(shader, len(log), None, log)
        assert ok.value, log.value.decode()
        G.glAttachShader(p, shader)
        shaders.append(shader)
    G.glLinkProgram(p)
    ok = I()
    G.glGetProgramiv(p, 0x8B82, C.byref(ok))
    log = C.create_string_buffer(8192)
    G.glGetProgramInfoLog(p, len(log), None, log)
    assert ok.value, log.value.decode()
    for shader in shaders:
        G.glDeleteShader(shader)
    return p

gpu = next(p for p in Path('/sys/bus/platform/devices').iterdir() if (p / 'gpu_busy_percent').exists())
freq = next(gpu.glob('devfreq/*/cur_freq'))
zones = [p for p in Path('/sys/class/thermal').glob('thermal_zone*') if 'gpu' in (p / 'type').read_text()]
def sample():
    return {'t': time.monotonic(), 'busy': int((gpu / 'gpu_busy_percent').read_text()),
            'hz': int(freq.read_text()), 'runtime': (gpu / 'power/runtime_status').read_text().strip(),
            'temperatures': {p.name: int((p / 'temp').read_text()) for p in zones}}

report = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'before': sample()}
display = E.eglGetDisplay(None)
major, minor = I(), I()
assert E.eglInitialize(display, C.byref(major), C.byref(minor))
assert E.eglBindAPI(0x30A0)
config, count = P(), I()
assert E.eglChooseConfig(display, array(0x3033, 1, 0x3040, 0x40, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3038), C.byref(config), 1, C.byref(count)) and count.value
surface = E.eglCreatePbufferSurface(display, config, array(0x3057, 512, 0x3056, 512, 0x3038))
context = E.eglCreateContext(display, config, None, array(0x3098, 3, 0x3038))
assert surface and context and E.eglMakeCurrent(display, surface, surface, context)
report['renderer'] = G.glGetString(0x1F01).decode()
report['version'] = G.glGetString(0x1F02).decode()
assert report['renderer'] == 'FD730', report['renderer']
vertex = '#version 300 es\nvoid main(){vec2 p=vec2((gl_VertexID<<1)&2,gl_VertexID&2);gl_Position=vec4(p*2.0-1.0,0.0,1.0);}'
fragment = '#version 300 es\nprecision highp float;out vec4 color;void main(){color=vec4(0.25,0.5,0.75,1.0);}'
basic = program([(0x8B31, vertex), (0x8B30, fragment)])
G.glViewport(0, 0, 512, 512)
G.glUseProgram(basic)
G.glDrawArrays(4, 0, 3)
pixels = (C.c_ubyte * (512 * 512 * 4))()
G.glReadPixels(0, 0, 512, 512, 0x1908, 0x1401, pixels)
assert G.glGetError() == 0
expected = [64, 128, 191, 255]
bad = sum(any(abs(pixels[i+j] - expected[j]) > 1 for j in range(4)) for i in range(0, len(pixels), 4))
assert bad == 0, bad
report['render_readback'] = {'pixels': 512 * 512, 'mismatches': bad, 'rgba': list(pixels[:4])}

compute = program([(0x91B9, '#version 310 es\nlayout(local_size_x=64) in;layout(std430,binding=0) buffer Out{uint values[];};void main(){uint i=gl_GlobalInvocationID.x;values[i]=i*3u+7u;}')])
buffer = U()
G.glGenBuffers(1, C.byref(buffer))
G.glBindBuffer(0x90D2, buffer)
G.glBufferData(0x90D2, 4096 * 4, None, 0x88E8)
G.glBindBufferBase(0x90D2, 0, buffer)
G.glUseProgram(compute)
G.glDispatchCompute(64, 1, 1)
G.glMemoryBarrier(0xFFFFFFFF)
G.glFinish()
pointer = G.glMapBufferRange(0x90D2, 0, 4096 * 4, 1)
assert pointer and G.glGetError() == 0
values = (U * 4096).from_address(pointer)
bad = sum(values[i] != i * 3 + 7 for i in range(4096))
assert G.glUnmapBuffer(0x90D2) and bad == 0
G.glDeleteBuffers(1, C.byref(buffer))
report['compute_readback'] = {'values': 4096, 'mismatches': bad}

heavy = program([(0x8B31, vertex), (0x8B30, '#version 300 es\nprecision highp float;out vec4 color;void main(){float x=gl_FragCoord.x*0.001+gl_FragCoord.y*0.003;for(int i=0;i<192;i++){x=sin(x*1.01+float(i)*0.001);}color=vec4(x*0.5+0.5,0.5,0.25,1.0);}')])
G.glUseProgram(heavy)
report['cycles'] = []
for cycle in range(1):
    start, next_sample, frames = time.monotonic(), 0, 0
    samples = []
    while time.monotonic() - start < 100:
        G.glDrawArrays(4, 0, 3)
        G.glFinish()
        assert G.glGetError() == 0
        frames += 1
        if time.monotonic() >= next_sample:
            point = sample()
            samples.append(point)
            assert max(point['temperatures'].values()) < 75000, point
            next_sample = time.monotonic() + 0.25
    elapsed = time.monotonic() - start
    time.sleep(2)
    report['cycles'].append({'seconds': elapsed, 'frames': frames, 'samples': samples, 'idle_after': sample()})
for p in [basic, compute, heavy]:
    G.glDeleteProgram(p)
assert E.eglMakeCurrent(display, None, None, None)
E.eglDestroyContext(display, context)
E.eglDestroySurface(display, surface)
E.eglTerminate(display)
time.sleep(2)
report['after'] = sample()
report['boot_id_after'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
assert report['boot_id_after'] == report['boot_id']
print(json.dumps(report, indent=2))
