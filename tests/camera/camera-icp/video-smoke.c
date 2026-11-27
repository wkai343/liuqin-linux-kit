/* SPDX-License-Identifier: MIT */
/* Direct V4L2 IPE lifecycle/pixel test. No module loading or boot changes.
 * Usage: video-smoke [--temporal] /dev/videoN WIDTH HEIGHT FRAMES [OUTPUT.nv12]
 * --temporal requires at least 30 frames, tests noise, motion and a scene cut.
 * FRAMES=0 checks format, buffer allocation and close without powering IPE.
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static void check(int result, const char *operation)
{
 if (result < 0) { perror(operation); exit(1); }
}
static void call(int fd, unsigned long cmd, void *arg, const char *name)
{
 int r;
 do { r = ioctl(fd, cmd, arg); } while (r < 0 && errno == EINTR);
 check(r, name);
}
static double now(void)
{
 struct timespec t;
 clock_gettime(CLOCK_MONOTONIC, &t);
 return t.tv_sec + t.tv_nsec / 1e9;
}
static void format(int fd, unsigned type, unsigned w, unsigned h)
{
 struct v4l2_format f = {.type = type};
 f.fmt.pix_mp.width = w; f.fmt.pix_mp.height = h;
 f.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
 if (V4L2_TYPE_IS_CAPTURE(type)) {
  /* The capture queue follows the selected input, including for TRY_FMT. */
  f.fmt.pix_mp.width = w == 640 ? 4080 : 640;
  f.fmt.pix_mp.height = h == 480 ? 3060 : 480;
 }
 struct v4l2_format trial = f;
 call(fd, VIDIOC_TRY_FMT, &trial, "TRY_FMT");
 call(fd, VIDIOC_S_FMT, &f, "S_FMT");
 if (memcmp(&trial.fmt.pix_mp, &f.fmt.pix_mp, sizeof(f.fmt.pix_mp)) ||
     f.fmt.pix_mp.width != w || f.fmt.pix_mp.height != h ||
     f.fmt.pix_mp.num_planes != 1 || f.fmt.pix_mp.plane_fmt[0].sizeimage != w*h*3/2)
  { fprintf(stderr, "Unexpected geometry\n"); exit(1); }
}
static void *allocate(int fd, unsigned type, size_t *size)
{
 struct v4l2_requestbuffers req = {.count = 1, .type = type, .memory = V4L2_MEMORY_MMAP};
 struct v4l2_plane plane = {0};
 struct v4l2_buffer b = {.type = type, .memory = V4L2_MEMORY_MMAP, .length = 1, .m.planes = &plane};
 call(fd, VIDIOC_REQBUFS, &req, "REQBUFS");
 if (req.count != 1) { fprintf(stderr, "Unexpected buffer count\n"); exit(1); }
 call(fd, VIDIOC_QUERYBUF, &b, "QUERYBUF");
 *size = plane.length;
 void *p = mmap(NULL, plane.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, plane.m.mem_offset);
 if (p == MAP_FAILED) { perror("mmap"); exit(1); }
 return p;
}
static void queue(int fd, unsigned type, unsigned bytes, unsigned sequence)
{
 struct v4l2_plane plane = {.bytesused = bytes, .length = bytes};
 struct v4l2_buffer b = {.type = type, .memory = V4L2_MEMORY_MMAP, .length = 1,
  .m.planes = &plane, .field = V4L2_FIELD_NONE};
 b.timestamp.tv_sec = sequence + 1;
 call(fd, VIDIOC_QBUF, &b, "QBUF");
}
static void dequeue(int fd, unsigned type, unsigned bytes, unsigned sequence)
{
 struct v4l2_plane plane = {0};
 struct v4l2_buffer b = {.type = type, .memory = V4L2_MEMORY_MMAP, .length = 1, .m.planes = &plane};
 call(fd, VIDIOC_DQBUF, &b, "DQBUF");
 if (b.flags & V4L2_BUF_FLAG_ERROR || plane.bytesused != bytes || b.timestamp.tv_sec != sequence + 1)
  { fprintf(stderr, "Frame error, size or timestamp mismatch: flags=%x bytes=%u time=%lld expected=%u\n",
    b.flags, plane.bytesused, (long long)b.timestamp.tv_sec, sequence + 1); exit(1); }
}
static int temporal_target(unsigned x, unsigned y, unsigned w, unsigned h, unsigned n)
{
 int level = (x < w / 2) ? 96 : 160;
 if (n >= 20) level = 256 - level;
 if (n >= 10 && n < 20) {
  unsigned left = (n - 10) * (w / 20);
  if (x >= left && x < left + w / 8 && y >= h / 3 && y < h / 3 + h / 4)
   level = 220;
 }
 return level;
}

int main(int argc, char **argv)
{
 int temporal = argc > 1 && !strcmp(argv[1], "--temporal");
 if (temporal) { argc--; argv++; }
 if (argc < 5 || argc > 6) return 2;
 unsigned w = strtoul(argv[2], NULL, 10), h = strtoul(argv[3], NULL, 10);
 unsigned frames = strtoul(argv[4], NULL, 10);
 if (temporal && frames && frames < 30) return 2;
 if (!((w == 640 && h == 480) || (w == 2592 && (h == 1472 || h == 1952)) || (w == 3840 && h == 2160) || (w == 4080 && (h == 2296 || h == 3060))) || frames > 100000) return 2;
 unsigned bytes = w*h*3/2, out = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, cap = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
 int fd = open(argv[1], O_RDWR | O_NONBLOCK); check(fd, "open");
 struct v4l2_capability caps;
 call(fd, VIDIOC_QUERYCAP, &caps, "QUERYCAP");
 if (strcmp((char *)caps.driver, "qcom-ipe")) { fprintf(stderr, "Not the IPE test node\n"); return 1; }
 format(fd, out, w, h); format(fd, cap, w, h);
 size_t insize, outsize;
 uint8_t *input = allocate(fd, out, &insize), *output = allocate(fd, cap, &outsize);
 if (!frames) {
  munmap(input, insize); munmap(output, outsize); close(fd);
  puts("{\"preflight\":true,\"hardware_started\":false}");
  return 0;
 }
 call(fd, VIDIOC_STREAMON, &out, "STREAMON output");
 call(fd, VIDIOC_STREAMON, &cap, "STREAMON capture");
 double started = now();
 for (unsigned n = 0; n < frames; n++) {
  for (unsigned y = 0; y < h; y++) for (unsigned x = 0; x < w; x++)
   if (temporal) {
    uint32_t rnd = (y * w + x) ^ ((n + 1) * 0x9e3779b9u);
    rnd ^= rnd >> 16; rnd *= 0x7feb352du; rnd ^= rnd >> 15; rnd *= 0x846ca68bu; rnd ^= rnd >> 16;
    input[y*w+x] = temporal_target(x, y, w, h, n) + (int)(rnd % 25) - 12;
   } else input[y*w+x] = 16 + ((x / 13 + y / 17 + n * 11) % 220);
  memset(input + w*h, 128, w*h/2); memset(output, 0xa5, bytes);
  queue(fd, out, bytes, n); queue(fd, cap, bytes, n);
  struct pollfd p = {.fd = fd, .events = POLLIN};
  int r = poll(&p, 1, 4000); check(r, "poll");
  if (!r || p.revents & (POLLERR | POLLHUP | POLLNVAL)) { fprintf(stderr, "Frame wait failed: %x\n", p.revents); return 1; }
  dequeue(fd, cap, bytes, n); dequeue(fd, out, bytes, n);
  if ((!temporal || !n) && memcmp(input, output, bytes)) {
   size_t i; for (i = 0; i < bytes && input[i] == output[i]; i++);
   fprintf(stderr, "Pixel mismatch frame=%u offset=%zu input=%u output=%u\n", n, i, input[i], output[i]); return 1;
  }
  if (temporal) {
   double input_mse = 0, output_mse = 0, bias = 0;
   unsigned maximum = 0;
   for (unsigned y = 0; y < h; y++) for (unsigned x = 0; x < w; x++) {
    int target = temporal_target(x, y, w, h, n);
    int a = input[y*w+x] - target, b = output[y*w+x] - target;
    input_mse += a*a; output_mse += b*b; bias += b;
    if ((unsigned)abs(b) > maximum) maximum = abs(b);
   }
   input_mse /= w*h; output_mse /= w*h; bias /= w*h;
   if (memcmp(input + w*h, output + w*h, w*h/2) || maximum > 25 ||
       bias < -2 || bias > 2 || ((n == 9 || n == 29) && output_mse >= input_mse * .7)) {
    fprintf(stderr, "Temporal quality failure frame=%u input_mse=%f output_mse=%f bias=%f max=%u\n",
      n, input_mse, output_mse, bias, maximum); return 1;
   }
   printf("{\"frame\":%u,\"input_mse\":%.4f,\"output_mse\":%.4f,\"bias\":%.4f,\"max_error\":%u}\n",
     n, input_mse, output_mse, bias, maximum);
  }
 }
 double elapsed = now() - started;
 call(fd, VIDIOC_STREAMOFF, &cap, "STREAMOFF capture");
 call(fd, VIDIOC_STREAMOFF, &out, "STREAMOFF output");
 if (argc == 6) {
  FILE *f = fopen(argv[5], "wb");
  if (!f || fwrite(output, bytes, 1, f) != 1 || fclose(f)) { perror("save"); return 1; }
 }
 munmap(input, insize); munmap(output, outsize); close(fd);
 printf("{\"frames\":%u,\"width\":%u,\"height\":%u,\"pixels_exact\":%s,\"temporal\":%s,\"seconds\":%.6f,\"fps\":%.3f}\n",
  frames, w, h, temporal ? "false" : "true", temporal ? "true" : "false", elapsed, frames/elapsed);
 return 0;
}
