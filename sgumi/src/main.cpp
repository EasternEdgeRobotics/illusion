// SGUMI -- the Super Graphic Ultra Modern Interface.
//
// illusion's desktop frontend for lipgloss.
//
// The window, the frame loop, the fatal-error reporting and the config file
// handling below are ported from Software_2027's apps/frontend/src/main.cpp at
// commit ff6a7f6. Everything related to ROVs was dropped. 
//
// See CMakeLists.txt for how to diff the bones against that repo later.

#include "Lipgloss.hpp"
#include "Paths.hpp"
#include "Theme.hpp"
#include "build_info.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>
#include "imgui.h"
#include "backends/imgui_impl_sdl3.h"
#include "backends/imgui_impl_sdlgpu3.h"

#include <nlohmann/json.hpp>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace {

// Matches StartupWMClass in the .desktop file CMakeLists.txt generates. If one
// changes the other has to, or xfce shows the running window as a second,
// unnamed taskbar entry beside the launcher.
constexpr const char* kAppId = "com.easternedgerobotics.sgumi";

// Fixed-size buffers because ImGui::InputText writes into a char array. 512 is
// what the 2027 frontend uses for the same job and is far past any real URL.
struct Config {
    char lipglossUrl[512] = "http://127.0.0.1:8081";
    char lipglossToken[512] = "";
};

Config g_config;

// ---------------------------------------------------------------------------
// Config file
// ---------------------------------------------------------------------------

// ImGui hands back a char array; json wants a std::string. Guarding on the key
// existing means a config written by an older build, missing a field added
// since, keeps the built-in default rather than becoming an empty string.
void copyJsonString(const json& data, const char* key, char* out, size_t size) {
    if (!data.contains(key) || !data[key].is_string()) {
        return;
    }

    const std::string value = data[key].get<std::string>();
    std::snprintf(out, size, "%s", value.c_str());
}

bool saveConfigToFile(const fs::path& path, const Config& config) {
    std::error_code ec;

    if (path.has_parent_path()) {
        fs::create_directories(path.parent_path(), ec);

        if (ec) {
            std::cerr << "Failed to create config directory: "
                      << path.parent_path() << std::endl;
            return false;
        }
    }

    std::ofstream output(path);

    if (!output) {
        std::cerr << "Failed to open config file for writing: " << path
                  << std::endl;
        return false;
    }

    const json data = {
        {"lipgloss_url", config.lipglossUrl},
        {"lipgloss_token", config.lipglossToken},
    };

    output << data.dump(4) << std::endl;
    return true;
}

bool loadConfigFromFile(const fs::path& path, Config& config) {
    if (!fs::exists(path)) {
        std::cerr << "Config file does not exist. Creating default config: "
                  << path << std::endl;

        saveConfigToFile(path, config);
        return false;
    }

    std::ifstream input(path);

    if (!input) {
        std::cerr << "Failed to open config file: " << path << std::endl;
        return false;
    }

    try {
        json data;
        input >> data;

        copyJsonString(data, "lipgloss_url", config.lipglossUrl,
                       sizeof(config.lipglossUrl));
        copyJsonString(data, "lipgloss_token", config.lipglossToken,
                       sizeof(config.lipglossToken));
    } catch (const json::exception& e) {
        // Not fatal. A corrupt config costs the saved URL, not the session --
        // the window still opens and the settings can be retyped.
        std::cerr << "Failed to parse config file: " << e.what() << std::endl;
        return false;
    }

    return true;
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

// Every fatal path below used to just print and return -1, which is fine at a
// terminal and useless anywhere there is no console to read -- a double-click
// on the kiosk's desktop, or an .app launched from Finder. So the same message
// also goes to a message box, which SDL renders natively.
//
// Before SDL_Init there is no video subsystem to put a box on, so the call
// fails and this degrades to the stderr line it always was.
void reportFatal(const char* stage, const char* detail) {
    std::cerr << stage << ": " << detail << std::endl;
    SDL_ShowSimpleMessageBox(SDL_MESSAGEBOX_ERROR, stage, detail, nullptr);
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

std::string formatUptime(long long ms) {
    if (ms <= 0) {
        return "unknown";
    }

    const long long seconds = ms / 1000;
    const long long days = seconds / 86400;
    const long long hours = (seconds % 86400) / 3600;
    const long long minutes = (seconds % 3600) / 60;

    char buffer[64];

    if (days > 0) {
        std::snprintf(buffer, sizeof(buffer), "%lldd %lldh %lldm",
                      days, hours, minutes);
    } else if (hours > 0) {
        std::snprintf(buffer, sizeof(buffer), "%lldh %lldm", hours, minutes);
    } else {
        std::snprintf(buffer, sizeof(buffer), "%lldm %llds",
                      minutes, seconds % 60);
    }

    return buffer;
}

// A coloured dot plus a word, rather than colouring the word itself: the
// status line is read from across the closet, and the dot survives being out
// of focus better than tinted text does.
void statusDot(const ImVec4& colour, const char* label) {
    ImGui::TextColored(colour, "%s", "\xe2\x97\x8f");  // U+25CF BLACK CIRCLE
    ImGui::SameLine();
    ImGui::TextUnformatted(label);
}

void drawConnectionStatus(const lipgloss::Snapshot& snapshot) {
    constexpr ImVec4 kGood { 0.35f, 0.80f, 0.45f, 1.0f };
    constexpr ImVec4 kWarn { 0.90f, 0.65f, 0.30f, 1.0f };
    constexpr ImVec4 kBad  { 0.88f, 0.36f, 0.36f, 1.0f };

    if (!snapshot.reachable) {
        statusDot(kBad, "lipgloss unreachable");
    } else if (snapshot.unauthorized) {
        statusDot(kWarn, "lipgloss up, token rejected");
    } else if (snapshot.paused) {
        statusDot(kWarn, "queue paused");
    } else {
        statusDot(kGood, "connected");
    }

    if (!snapshot.error.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(snapshot.unauthorized ? kWarn : kBad, "%s",
                           snapshot.error.c_str());
        ImGui::PopTextWrapPos();
    }

    if (!snapshot.reachable) {
        return;
    }

    ImGui::Spacing();

    if (ImGui::BeginTable("health", 2, ImGuiTableFlags_SizingFixedFit)) {
        auto row = [](const char* key, const std::string& value) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextDisabled("%s", key);
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(value.c_str());
        };

        row("Version", snapshot.version);
        row("Model", snapshot.model);
        row("Printer port", snapshot.printerPort);
        row("Uptime", formatUptime(snapshot.uptimeMs));

        ImGui::EndTable();
    }
}

void drawQueue(const lipgloss::Snapshot& snapshot) {
    if (!snapshot.reachable || snapshot.unauthorized) {
        ImGui::TextDisabled("Queue unavailable.");
        return;
    }

    if (!snapshot.title.empty()) {
        ImGui::TextUnformatted(snapshot.title.c_str());
    }

    if (!snapshot.description.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextDisabled("%s", snapshot.description.c_str());
        ImGui::PopTextWrapPos();
    }

    if (snapshot.jobs.empty()) {
        return;
    }

    ImGui::Spacing();

    constexpr ImGuiTableFlags kFlags =
        ImGuiTableFlags_Borders |
        ImGuiTableFlags_RowBg |
        ImGuiTableFlags_SizingStretchProp |
        ImGuiTableFlags_ScrollY;

    // Height is bounded so a hundred-job queue cannot push the window off the
    // screen; ScrollY above is what makes the overflow reachable.
    const float height = ImGui::GetTextLineHeightWithSpacing() * 12.0f;

    if (ImGui::BeginTable("jobs", 5, kFlags, ImVec2(0.0f, height))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Job", ImGuiTableColumnFlags_WidthFixed);
        ImGui::TableSetupColumn("Description");
        ImGui::TableSetupColumn("Labels", ImGuiTableColumnFlags_WidthFixed);
        ImGui::TableSetupColumn("Source", ImGuiTableColumnFlags_WidthFixed);
        ImGui::TableSetupColumn("State", ImGuiTableColumnFlags_WidthFixed);
        ImGui::TableHeadersRow();

        for (const lipgloss::Job& job : snapshot.jobs) {
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.jobId.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.description.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.labels.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.source.c_str());
            ImGui::TableNextColumn();
            ImGui::TextUnformatted(job.state.c_str());
        }

        ImGui::EndTable();
    }
}

int runSgumi() {
    SDL_SetAppMetadata("SGUMI", SGUMI_VERSION, kAppId);

    // Useful on Linux desktops -- this is what xfce matches against the
    // .desktop file to give the window its name and icon.
    SDL_SetHint(SDL_HINT_APP_ID, kAppId);

    // No gamepad subsystem, unlike the 2027 frontend: nothing here is flown.
    if (!SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS)) {
        reportFatal("SDL_Init failed", SDL_GetError());
        return -1;
    }

    const SDL_WindowFlags windowFlags =
        SDL_WINDOW_RESIZABLE |
        SDL_WINDOW_HIGH_PIXEL_DENSITY;

    SDL_Window* window = SDL_CreateWindow("SGUMI", 900, 700, windowFlags);

    if (!window) {
        reportFatal("SDL_CreateWindow failed", SDL_GetError());
        SDL_Quit();
        return -1;
    }

    // No DXIL here. The 2027 frontend advertises it because its NV12 pipeline
    // ships precompiled DXIL blobs; SGUMI compiles no shaders of its own, and
    // ImGui's SDL_GPU backend carries whatever it needs for every backend.
    const SDL_GPUShaderFormat shaderFormats =
        SDL_GPU_SHADERFORMAT_SPIRV |
        SDL_GPU_SHADERFORMAT_MSL |
        SDL_GPU_SHADERFORMAT_DXIL;

    // The D3D12 debug layer needs the "Graphics Tools" optional Windows
    // feature installed; asking for it without that present fails device
    // creation outright.
#if defined(NDEBUG)
    const bool gpuDebugMode = false;
#else
    const bool gpuDebugMode = true;
#endif

    SDL_GPUDevice* gpuDevice =
        SDL_CreateGPUDevice(shaderFormats, gpuDebugMode, nullptr);

    if (!gpuDevice) {
        reportFatal("SDL_CreateGPUDevice failed", SDL_GetError());
        SDL_DestroyWindow(window);
        SDL_Quit();
        return -1;
    }

    if (!SDL_ClaimWindowForGPUDevice(gpuDevice, window)) {
        reportFatal("SDL_ClaimWindowForGPUDevice failed", SDL_GetError());
        SDL_DestroyGPUDevice(gpuDevice);
        SDL_DestroyWindow(window);
        SDL_Quit();
        return -1;
    }

    SDL_SetGPUSwapchainParameters(
        gpuDevice,
        window,
        SDL_GPU_SWAPCHAINCOMPOSITION_SDR,
        SDL_GPU_PRESENTMODE_VSYNC);

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGui::GetIO().IniFilename = paths::imguiIniPath();
    ImGui::StyleColorsDark();
    applyTheme();  // see src/Theme.cpp to tweak

    ImGui_ImplSDL3_InitForSDLGPU(window);

    ImGui_ImplSDLGPU3_InitInfo initInfo = {};
    initInfo.Device = gpuDevice;
    initInfo.ColorTargetFormat =
        SDL_GetGPUSwapchainTextureFormat(gpuDevice, window);
    initInfo.MSAASamples = SDL_GPU_SAMPLECOUNT_1;
    ImGui_ImplSDLGPU3_Init(&initInfo);

    const fs::path configPath = paths::defaultConfigPath();

    if (loadConfigFromFile(configPath, g_config)) {
        std::cout << "Loaded config file: " << configPath << std::endl;
    } else {
        std::cout << "Using default config. Config path: " << configPath
                  << std::endl;
    }

    lipgloss::Client client;
    client.setEndpoint(g_config.lipglossUrl, g_config.lipglossToken);
    client.start();

    bool showSettings = g_config.lipglossToken[0] == '\0';
#if defined(SGUMI_THEME_EDITOR)
    bool showThemeEditor = false;
#endif
    std::string configStatus;

    bool done = false;

    while (!done) {
        SDL_Event event;

        while (SDL_PollEvent(&event)) {
            ImGui_ImplSDL3_ProcessEvent(&event);

            if (event.type == SDL_EVENT_QUIT) {
                done = true;
            }

            if (event.type == SDL_EVENT_WINDOW_CLOSE_REQUESTED &&
                event.window.windowID == SDL_GetWindowID(window)) {
                done = true;
            }
        }

        if (SDL_GetWindowFlags(window) & SDL_WINDOW_MINIMIZED) {
            SDL_Delay(10);
            continue;
        }

        ImGui_ImplSDLGPU3_NewFrame();
        ImGui_ImplSDL3_NewFrame();
        ImGui::NewFrame();

#if defined(SGUMI_THEME_EDITOR)
        if (ImGui::IsKeyPressed(ImGuiKey_F10, false)) {
            showThemeEditor = !showThemeEditor;
        }
#endif

        // One snapshot per frame, taken once. Calling client.snapshot() from
        // each draw function would take the worker's lock several times and
        // could show two different polls in one frame.
        const lipgloss::Snapshot snapshot = client.snapshot();

        // The main window fills the OS window and is not movable: there is
        // only one, and on a kiosk a draggable panel is something to
        // accidentally shove off-screen, not a feature.
        const ImGuiViewport* viewport = ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(viewport->WorkPos);
        ImGui::SetNextWindowSize(viewport->WorkSize);

        constexpr ImGuiWindowFlags kMainFlags =
            ImGuiWindowFlags_NoTitleBar |
            ImGuiWindowFlags_NoResize |
            ImGuiWindowFlags_NoMove |
            ImGuiWindowFlags_NoCollapse |
            ImGuiWindowFlags_NoBringToFrontOnFocus |
            ImGuiWindowFlags_MenuBar;

        if (ImGui::Begin("##main", nullptr, kMainFlags)) {
            if (ImGui::BeginMenuBar()) {
                if (ImGui::BeginMenu("SGUMI")) {
                    if (ImGui::MenuItem("Settings")) {
                        showSettings = true;
                    }

                    if (ImGui::MenuItem("Refresh now")) {
                        client.refresh();
                    }

                    ImGui::Separator();

                    if (ImGui::MenuItem("Quit")) {
                        done = true;
                    }

                    ImGui::EndMenu();
                }

                ImGui::EndMenuBar();
            }

            ImGui::SeparatorText("Connection");
            drawConnectionStatus(snapshot);

            ImGui::Spacing();
            ImGui::SeparatorText("Print queue");
            drawQueue(snapshot);

            ImGui::Spacing();
            ImGui::TextDisabled(
                "Printing is not wired up yet.");
        }

        ImGui::End();

        if (showSettings) {
            ImGui::SetNextWindowSize(ImVec2(750, 400), ImGuiCond_FirstUseEver);

            if (ImGui::Begin("Settings", &showSettings)) {
                if (ImGui::BeginTabBar("Config Tabs")) {
                    if (ImGui::BeginTabItem("Lipgloss")) {
                        ImGui::TextDisabled("Must match lipgloss.yaml on the print server.");
                        ImGui::Spacing();

                        ImGui::InputText("lipgloss URL", g_config.lipglossUrl,
                                        sizeof(g_config.lipglossUrl));
                        ImGui::InputText("Token", g_config.lipglossToken,
                                        sizeof(g_config.lipglossToken),
                                        ImGuiInputTextFlags_Password);

                        ImGui::Spacing();

                        if (ImGui::Button("Apply and save")) {
                            // Applied to the live client either way: a token that
                            // works is worth having for this session even if the disk
                            // write failed.
                            client.setEndpoint(g_config.lipglossUrl,
                                            g_config.lipglossToken);

                            configStatus = saveConfigToFile(configPath, g_config)
                                            ? "Saved to " + configPath.string()
                                            : "Failed to write " + configPath.string();
                        }

                        ImGui::SameLine();

                        if (ImGui::Button("Apply without saving")) {
                            client.setEndpoint(g_config.lipglossUrl,
                                            g_config.lipglossToken);
                            configStatus = "Applied for this session only.";
                        }

                        if (!configStatus.empty()) {
                            ImGui::Spacing();
                            ImGui::PushTextWrapPos(0.0f);
                            ImGui::TextDisabled("%s", configStatus.c_str());
                            ImGui::PopTextWrapPos();
                        }

                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("About")) {
                        ImGui::TextUnformatted("Super Graphic Ultra Modern Interface");
                        ImGui::TextDisabled("illusion's frontend for lipgloss");
                        ImGui::Separator();
                        ImGui::Text("Version %s", SGUMI_VERSION);
                        ImGui::Text("Built %s", EER_BUILD_DATE);
                        ImGui::Text("Renderer %s",
                                    SDL_GetGPUDeviceDriver(gpuDevice));
                        ImGui::Separator();
                        ImGui::TextDisabled("Config: %s", configPath.string().c_str());

                        ImGui::EndTabItem();
                    }
                }
                ImGui::EndTabBar();
            }

            ImGui::End();
        }

#if defined(SGUMI_THEME_EDITOR)
        if (showThemeEditor) {
            drawThemeEditor(&showThemeEditor);
        }
#endif

        ImGui::Render();
        ImDrawData* drawData = ImGui::GetDrawData();

        SDL_GPUCommandBuffer* cmd = SDL_AcquireGPUCommandBuffer(gpuDevice);

        SDL_GPUTexture* swapchainTex = nullptr;
        SDL_WaitAndAcquireGPUSwapchainTexture(
            cmd, window, &swapchainTex, nullptr, nullptr);

        if (swapchainTex) {
            ImGui_ImplSDLGPU3_PrepareDrawData(drawData, cmd);

            SDL_GPUColorTargetInfo target = {};
            target.texture = swapchainTex;
            target.load_op = SDL_GPU_LOADOP_CLEAR;
            target.store_op = SDL_GPU_STOREOP_STORE;
            target.clear_color = SDL_FColor { 0.08f, 0.09f, 0.11f, 1.0f };

            SDL_GPURenderPass* pass =
                SDL_BeginGPURenderPass(cmd, &target, 1, nullptr);
            ImGui_ImplSDLGPU3_RenderDrawData(drawData, cmd, pass);
            SDL_EndGPURenderPass(pass);
        }

        SDL_SubmitGPUCommandBuffer(cmd);
    }

    // Before the GPU teardown below: the worker holds no GPU resources, but it
    // does hold a socket, and joining it here keeps shutdown ordered rather
    // than relying on the destructor firing at the right moment.
    client.stop();

    SDL_WaitForGPUIdle(gpuDevice);

    ImGui_ImplSDLGPU3_Shutdown();
    ImGui_ImplSDL3_Shutdown();
    ImGui::DestroyContext();

    SDL_ReleaseWindowFromGPUDevice(gpuDevice, window);
    SDL_DestroyGPUDevice(gpuDevice);
    SDL_DestroyWindow(window);
    SDL_Quit();

    return 0;
}

}  // namespace

// On Windows, SDL_main.h redefines main() to SDL_main and SDL supplies the
// real WinMain, which calls SDL_main(int, char**). The signature has to match
// exactly or the link fails, which is why argc/argv are named and unused
// rather than omitted.
int main(int argc, char** argv) {
    (void)argc;
    (void)argv;

    return runSgumi();
}
