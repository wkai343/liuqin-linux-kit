/* SPDX-License-Identifier: MIT
 * Fixed-exposure HDR input capture. No HDR merge is performed here.
 * Build: c++ -std=c++17 -O2 camera-hdr-capture.cpp -o camera-hdr-capture \
 *        $(pkg-config --cflags --libs libcamera libcrypto) -pthread
 */
#include <libcamera/libcamera.h>
#include <linux/dma-buf.h>
#include <openssl/evp.h>
#include <memory>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <sstream>
#include <algorithm>
#include <vector>

using namespace libcamera;
namespace fs = std::filesystem;

class Capture {
public:
 std::shared_ptr<Camera> camera;
 Stream *stream{};
 std::mutex mutex;
 std::condition_variable cv;
 unsigned count{}, limit{}, generation{};
 bool done{}, failed{}, fixed{};
 int exposure{};
 float gain{};
 std::array<float, 2> wb{1, 1};
 std::array<float, 9> ccm{};
 bool haveCcm{};
 fs::path directory;
 std::ofstream ledger;
 uint64_t lastTimestamp{};
 uint32_t lastSequence{};
 std::string fixedMetadata;

 void holdFocus(Request *request)
 {
  if (fixed && camera->controls().count(&controls::AfMode)) {
   request->controls().set(controls::AfMode, controls::AfModeAuto);
   request->controls().set(controls::AfTrigger, controls::AfTriggerCancel);
  }
 }

 std::string save(FrameBuffer *buffer, const fs::path &path)
 {
  std::ofstream output(path, std::ios::binary | std::ios::out);
  if (!output) throw std::runtime_error("open image");
  std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> hash(EVP_MD_CTX_new(), EVP_MD_CTX_free);
  if (!hash || !EVP_DigestInit_ex(hash.get(), EVP_sha256(), nullptr))
   throw std::runtime_error("SHA-256 initialization");
  for (unsigned i = 0; i < buffer->planes().size(); ++i) {
   const auto &p = buffer->planes()[i];
   size_t length = buffer->metadata().planes()[i].bytesused;
   if (!length || length > p.length || p.offset == FrameBuffer::Plane::kInvalidOffset)
    throw std::runtime_error("invalid image plane");
   dma_buf_sync sync{DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ};
   if (ioctl(p.fd.get(), DMA_BUF_IOCTL_SYNC, &sync))
    throw std::runtime_error("DMA read begin");
   void *map = mmap(nullptr, p.offset + length, PROT_READ, MAP_SHARED, p.fd.get(), 0);
   if (map == MAP_FAILED) {
    sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ;
    ioctl(p.fd.get(), DMA_BUF_IOCTL_SYNC, &sync);
    throw std::runtime_error("mmap image");
   }
   const char *pixels = static_cast<const char *>(map) + p.offset;
   output.write(pixels, length);
   bool hashed = EVP_DigestUpdate(hash.get(), pixels, length);
   munmap(map, p.offset + length);
   sync.flags = DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ;
   if (ioctl(p.fd.get(), DMA_BUF_IOCTL_SYNC, &sync) || !output || !hashed)
    throw std::runtime_error("DMA read end / image write");
  }
  output.close();
  unsigned char digest[EVP_MAX_MD_SIZE];
  unsigned size;
  if (!output || !EVP_DigestFinal_ex(hash.get(), digest, &size) || size != 32)
   throw std::runtime_error("image close / SHA-256");
  std::ostringstream hex;
  hex << std::hex << std::setfill('0');
  for (unsigned i = 0; i < size; ++i) hex << std::setw(2) << unsigned(digest[i]);
  return hex.str();
 }

 void complete(Request *request)
 {
  std::lock_guard<std::mutex> lock(mutex);
  if (done || request->status() == Request::RequestCancelled) return;
  try {
   auto *buffer = request->findBuffer(stream);
   const auto &m = buffer->metadata();
   const auto &controls = request->metadata();
   if (m.status != FrameMetadata::FrameSuccess || !m.timestamp ||
       (count && (m.timestamp <= lastTimestamp || m.sequence <= lastSequence)))
    throw std::runtime_error("invalid/non-monotonic frame identity");
   lastTimestamp = m.timestamp;
   lastSequence = m.sequence;
   auto sensorTimestamp = controls.get(libcamera::controls::SensorTimestamp);
   /* V4L2 timeval transport rounds the converter timestamp to microseconds.
    * Keep the original nanoseconds; never treat the IPE sequence as a sensor ID. */
   if (!sensorTimestamp || *sensorTimestamp <= 0 ||
       uint64_t(*sensorTimestamp) / 1000 != m.timestamp / 1000)
    throw std::runtime_error("sensor/output timestamp mismatch");
   auto e = controls.get(libcamera::controls::ExposureTime);
   auto g = controls.get(libcamera::controls::AnalogueGain);
   auto w = controls.get(libcamera::controls::ColourGains);
   if (!e || !g || !w || *e <= 0 || *g < 1)
    throw std::runtime_error("missing exposure metadata");
   exposure = *e; gain = *g; wb = {(*w)[0], (*w)[1]};
   if (auto matrix = controls.get(libcamera::controls::ColourCorrectionMatrix)) {
    std::copy(matrix->begin(), matrix->end(), ccm.begin()); haveCcm = true;
   }
   std::ostringstream settings;
   settings << std::setprecision(9) << *e << ',' << *g << ',' << (*w)[0] << ',' << (*w)[1];
   for (float v : ccm) settings << ',' << v;
   if (fixed && count && settings.str() != fixedMetadata)
    throw std::runtime_error("exposure/WB/CCM changed inside fixed stream");
   fixedMetadata = settings.str();
   ++count;
   std::string image, digest;
   if (fixed && (count == 1 || count == limit)) {
    image = "leg-" + std::to_string(generation) + "-frame-" + std::to_string(m.sequence) + ".nv12";
    digest = save(buffer, directory / image);
   }
   ledger << "{\"generation\":" << generation << ",\"ordinal\":" << count
          << ",\"sequence\":" << m.sequence << ",\"timestamp_ns\":" << m.timestamp
          << ",\"sensor_timestamp_ns\":" << *sensorTimestamp
          << ",\"request_sequence\":" << request->sequence()
          << ",\"cookie\":" << request->cookie()
          << ",\"fixed\":" << (fixed ? "true" : "false")
          << ",\"exposure_us\":" << *e << ",\"analogue_gain\":" << *g
          << ",\"colour_gains\":[" << (*w)[0] << ',' << (*w)[1] << "]"
          << ",\"ccm\":[";
   for (unsigned i = 0; i < ccm.size(); ++i) ledger << (i ? "," : "") << ccm[i];
   ledger << "],\"image\":\"" << image << "\",\"image_sha256\":\"" << digest << "\"}\n";
   if (!ledger) throw std::runtime_error("ledger write");
   if (count == limit) { done = true; cv.notify_all(); return; }
   request->reuse(Request::ReuseBuffers);
   holdFocus(request);
   if (camera->queueRequest(request)) throw std::runtime_error("requeue");
  } catch (const std::exception &e) {
   std::cerr << e.what() << '\n'; failed = done = true; cv.notify_all();
  }
 }

 void run(std::vector<std::unique_ptr<Request>> &requests, ControlList &start, bool manual, unsigned frames)
 {
  { std::lock_guard<std::mutex> lock(mutex);
    fixed = manual; count = 0; limit = frames; done = failed = false;
    lastTimestamp = 0; lastSequence = 0; fixedMetadata.clear(); }
  if (camera->start(&start)) throw std::runtime_error("start");
  bool queued = true;
  for (auto &r : requests) {
   r->reuse(Request::ReuseBuffers); holdFocus(r.get());
   if (camera->queueRequest(r.get())) { queued = false; break; }
  }
  std::unique_lock<std::mutex> lock(mutex);
  bool completed = queued && cv.wait_for(lock, std::chrono::seconds(30), [&]{ return done; });
  done = true;
  lock.unlock();
  int ret = camera->stop();
  if (!completed || failed || ret) throw std::runtime_error("capture/stop failed");
  ledger.flush();
 }
};

int main(int argc, char **argv)
{
 try {
  if (argc != 3 && argc != 4) throw std::runtime_error("usage: camera-hdr-capture front|rear NEW_OUTPUT_DIRECTORY [4080x2296|3840x2160]");
  std::string sensor = argv[1];
  if (sensor != "front" && sensor != "rear") throw std::runtime_error("sensor must be front or rear");
  Size requested = sensor == "front" ? Size(2592, 1952) : Size(4080, 3060);
  if (argc == 4) {
   std::string mode = argv[3];
   if (sensor != "rear") throw std::runtime_error("wide modes require the rear camera");
   if (mode == "4080x2296") requested = Size(4080, 2296);
   else if (mode == "3840x2160") requested = Size(3840, 2160);
   else throw std::runtime_error("unsupported rear sensor mode");
  }
  bool uhd = requested == Size(3840, 2160);
  const int64_t frameUs = uhd ? 16667 : 33334;
  std::array<int, 3> times = uhd ? std::array<int, 3>{1000, 4000, 16000} : std::array<int, 3>{2000, 8000, 32000};
  if (argc == 4 && setenv("LIBCAMERA_LIUQIN_IFE_ONLY", "1", 1)) throw std::runtime_error("select direct IFE");
  const char *direct = getenv("LIBCAMERA_LIUQIN_IFE_ONLY");
  fs::path output = argv[2];
  if (!fs::create_directory(output)) throw std::runtime_error("output must not exist");
  CameraManager manager;
  if (manager.start()) throw std::runtime_error("camera manager");
  std::ifstream maps("/proc/self/maps");
  std::ofstream libraries(output / "libraries.txt");
  for (std::string line; std::getline(maps, line);)
   if (line.find("/libcamera.so.") != std::string::npos) libraries << line << '\n';
  Capture c;
  for (auto &camera : manager.cameras())
   if (camera->properties().get(properties::Model).value_or("").find(sensor == "front" ? "imx596" : "s5kjn1") != std::string::npos) c.camera = camera;
  if (!c.camera || c.camera->acquire()) throw std::runtime_error("camera acquire");
  auto config = c.camera->generateConfiguration({StreamRole::StillCapture});
  if (!config) throw std::runtime_error("configuration");
  auto &s = config->at(0);
  s.pixelFormat = formats::NV12;
  s.size = requested;
  s.bufferCount = 4;
  if (config->validate() == CameraConfiguration::Invalid || s.size != requested || s.pixelFormat != formats::NV12 || c.camera->configure(config.get()))
   throw std::runtime_error("NV12 configure");
  c.stream = s.stream(); c.directory = output;
  c.ledger.open(output / "frames.jsonl"); c.ledger << std::setprecision(9);
  std::string bootId;
  std::ifstream("/proc/sys/kernel/random/boot_id") >> bootId;
  std::ofstream info(output / "capture.json");
  info << "{\"schema\":2,\"sensor\":\"" << sensor << "\",\"width\":" << s.size.width
       << ",\"height\":" << s.size.height << ",\"stride\":" << s.stride
       << ",\"frame_size\":" << s.frameSize
       << ",\"boot_id\":\"" << bootId << "\",\"output_sequence_kind\":\"" << (direct && std::string(direct) == "1" ? "ife" : "converter") << "\""
       << ",\"exposure_basis\":\"fixed-before-stream-on\",\"hdr_merged\":false,\"requested_us\":[" << times[0] << "," << times[1] << "," << times[2] << "]}\n";
  info.close();
  if (!info) throw std::runtime_error("capture manifest write");
  FrameBufferAllocator allocator(c.camera);
  if (allocator.allocate(c.stream) < 0) throw std::runtime_error("allocate");
  std::vector<std::unique_ptr<Request>> requests;
  for (const auto &b : allocator.buffers(c.stream)) {
   auto r = c.camera->createRequest(requests.size());
   if (!r || r->addBuffer(c.stream, b.get())) throw std::runtime_error("request");
   requests.push_back(std::move(r));
  }
  c.camera->requestCompleted.connect(&c, &Capture::complete);
  ControlList automatic(c.camera->controls());
  c.run(requests, automatic, false, 150);
  const float gain = c.gain;
  const auto wb = c.wb;
  const auto ccm = c.ccm;
  for (int exposure : times) {
   ++c.generation;
   ControlList manual(c.camera->controls());
   manual.set(controls::AeEnable, false); manual.set(controls::AwbEnable, false);
   if (c.camera->controls().count(&controls::AfMode)) {
    manual.set(controls::AfMode, controls::AfModeAuto);
    manual.set(controls::AfTrigger, controls::AfTriggerCancel);
   }
   manual.set(controls::ExposureTime, exposure); manual.set(controls::AnalogueGain, gain);
   manual.set(controls::ColourGains, wb);
   if (c.haveCcm) manual.set(controls::ColourCorrectionMatrix, ccm);
   const std::array<int64_t, 2> duration{frameUs, frameUs};
   manual.set(controls::FrameDurationLimits, duration);
   c.run(requests, manual, true, 16);
  }
  c.camera->requestCompleted.disconnect(&c);
  requests.clear(); allocator.free(c.stream); c.camera->release();
  c.ledger.close();
  if (!c.ledger) throw std::runtime_error("frame ledger close");
  std::ofstream marker(output / "COMPLETE");
  marker << "fixed exposure input capture only; not an HDR image\n";
  marker.close();
  if (!marker) throw std::runtime_error("capture marker write");
  std::cout << "PASS: three stopped-stream exposure legs and frame ledger\n";
 } catch (const std::exception &e) {
  std::cerr << "FAIL: " << e.what() << '\n'; return 1;
 }
}
