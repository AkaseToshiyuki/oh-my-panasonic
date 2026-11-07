#include <openvino/openvino.hpp>
#include <openssl/evp.h>
#include <algorithm>
#include <array>
#include <cstdio>
#include <fstream>
#include <future>
#include <iomanip>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>

namespace {
constexpr const char *cache = "/var/cache/pou-face-gpu/";
constexpr const char *detector = "bc24bb349491481c3ca793cf89306723162c280cb284c5a5e49df3760bf5c2ce";
constexpr const char *embedder = "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43";
void verify(std::istream &input, const std::string &expected) {
    if (!input || expected.size() != 64)
        throw std::runtime_error("Missing model or digest");
    std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> hash(EVP_MD_CTX_new(), EVP_MD_CTX_free);
    if (!hash || EVP_DigestInit_ex(hash.get(), EVP_sha256(), nullptr) != 1)
        throw std::runtime_error("SHA initialization");
    std::array<char, 131072> block;
    while (input) {
        input.read(block.data(), block.size());
        if (EVP_DigestUpdate(hash.get(), block.data(), input.gcount()) != 1)
            throw std::runtime_error("SHA update");
    }
    unsigned char digest[32];
    unsigned int len = 0;
    if (!input.eof() || EVP_DigestFinal_ex(hash.get(), digest, &len) != 1 || len != 32)
        throw std::runtime_error("SHA read failed");
    std::ostringstream hex;
    for (auto byte : digest)
        hex << std::hex << std::setw(2) << std::setfill('0') << int(byte);
    if (hex.str() != expected)
        throw std::runtime_error("Model digest mismatch");
}
ov::Core &core() {
    static ov::Core instance;
    return instance;
}
struct Network {
    ov::CompiledModel model;
    ov::InferRequest request;
    size_t input_count;
    Network(const char *source, int side) {
        if (side != 112 && side != 640)
            throw std::runtime_error("Unsupported model");
        auto original = std::async(std::launch::async, [source, side] {
            std::ifstream file(source, std::ios::binary);
            verify(file, side == 112 ? embedder : detector);
        });
        // Provisioning publishes both artifacts together through the root-owned current directory.
        std::string base = std::string(cache) + "current/model-" + std::to_string(side);
        std::ifstream blob(base, std::ios::binary), manifest(base + ".sha256");
        std::string digest;
        manifest >> digest;
        verify(blob, digest);
        blob.clear();
        blob.seekg(0);
        model = core().import_model(blob, "GPU.0");
        original.get();
        const auto devices = model.get_property(ov::execution_devices);
        if (devices.empty() || !std::all_of(devices.begin(), devices.end(), [](const auto &d) {
                return d == "GPU" || d == "GPU.0";
            }))
            throw std::runtime_error("GPU device mismatch");
        request = model.create_infer_request();
        input_count = request.get_input_tensor().get_size();
        if (input_count != size_t(3 * side * side))
            throw std::runtime_error("Input dimensions");
    }
};
} // namespace
extern "C" void *pou_ov_create(const char *path, int side) noexcept {
    try {
        return new Network(path, side);
    } catch (const std::exception &e) {
        std::fprintf(stderr, "OpenVINO load: %s\n", e.what());
        return nullptr;
    } catch (...) {
        return nullptr;
    }
}
extern "C" void pou_ov_destroy(void *p) noexcept {
    delete static_cast<Network *>(p);
}
extern "C" int pou_ov_run(void *p, const float *data, size_t n) noexcept {
    try {
        auto *net = static_cast<Network *>(p);
        if (n != net->input_count)
            return -1;
        auto in = net->request.get_input_tensor();
        if (in.get_element_type() != ov::element::f32)
            return -1;
        std::copy_n(data, n, in.data<float>());
        net->request.infer();
        return int(net->model.outputs().size());
    } catch (...) {
        return -1;
    }
}
extern "C" const float *pou_ov_output(void *p, size_t i, size_t *n) noexcept {
    try {
        auto t = static_cast<Network *>(p)->request.get_output_tensor(i);
        if (t.get_element_type() != ov::element::f32)
            return nullptr;
        *n = t.get_size();
        return t.data<const float>();
    } catch (...) {
        return nullptr;
    }
}