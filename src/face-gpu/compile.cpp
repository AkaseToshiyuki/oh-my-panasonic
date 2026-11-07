#include <openvino/openvino.hpp>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char **argv) {
    if (argc != 2)
        return 2;
    try {
        ov::Core core;
        const std::filesystem::path dir(argv[1]);
        for (int side : {640, 112}) {
            const std::string name = "model-" + std::to_string(side);
            auto net = core.read_model((dir / (name + ".onnx")).string());
            net->reshape(ov::PartialShape{1, 3, side, side});
            auto model =
                core.compile_model(net, "GPU.0", ov::hint::inference_precision(ov::element::f32),
                                   ov::hint::performance_mode(ov::hint::PerformanceMode::LATENCY));
            std::ofstream output(dir / name, std::ios::binary);
            output.exceptions(std::ios::badbit | std::ios::failbit);
            model.export_model(output);
            output.close();
        }
    } catch (const std::exception &e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
