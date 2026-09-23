// SGUMI -- The Super Graphic Ultra Modern Interface.
//
// illusion's desktop frontend for lipgloss.
//
// The window, the frame loop, the fatal-error reporting and the config file
// handling below are ported from Software_2027's apps/frontend/src/main.cpp at
// commit ff6a7f6. Everything related to ROVs was dropped. 
//
// See CMakeLists.txt for how to diff the bones against that repo later.

#include "Claws.hpp"
#include "Http.hpp"
#include "Image.hpp"
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

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
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

    // claws lives on the NAS VM in production, so this default is only right
    // for a development checkout running all four services locally. See
    // kiosk.example.yaml for what the closet laptop actually points at.
    char clawsUrl[512] = "http://127.0.0.1:8080";
    char clawsToken[512] = "";
};

Config g_config;

// The print form. Separate from Config because none of it is persisted -- a
// label typed yesterday is not something to restore on launch.
struct PrintForm {
    int styleIndex = 0;
    char sku[64] = "";
    char line1[128] = "";
    char line2[128] = "";
    int copies = 1;

    // The bot's get_text_from_sku, as a toggle: while on, line 1 is the item
    // name claws returns rather than anything typed. On by default because
    // that is the whole point of SGUMI knowing about claws -- scan, name
    // appears, print -- and it is trivially switched off for a one-off label.
    bool useSkuName = true;

    // Which lookup has already been copied into line 1, so the fill happens
    // once per lookup rather than every frame -- otherwise editing line 1
    // would be undone on the next frame.
    std::string filledFromSku;

    // requestSignature() of whatever the showing preview was rendered from, so
    // an edit since can be pointed out rather than leaving a stale picture
    // looking current.
    std::string previewOf;

    // Whether the picture on screen is the example rather than the form's own
    // label. Suppresses the staleness note, which would otherwise fire
    // immediately -- the example never matches the form.
    bool previewIsExample = false;

    // Latched once per session. Without it, anything that clears the preview
    // would bring the example back after the user had moved on from it.
    bool exampleRequested = false;

    // Range mode: one label per SKU across a span, instead of one label.
    // Swaps out most of the form, so it is a mode rather than a second panel.
    bool rangeMode = false;

    // Held as text, not numbers. The endpoint wants integers, but a SKU is
    // what is printed on the bin and what a scanner types, so these accept
    // either "EER-000421" or "421" and skuNumber() pulls the number out.
    char rangeFrom[64] = "";
    char rangeTo[64] = "";
};

PrintForm g_print;

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
        {"claws_url", config.clawsUrl},
        {"claws_token", config.clawsToken},
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
        copyJsonString(data, "claws_url", config.clawsUrl,
                       sizeof(config.clawsUrl));
        copyJsonString(data, "claws_token", config.clawsToken,
                       sizeof(config.clawsToken));
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

// ---------------------------------------------------------------------------
// Label styles
// ---------------------------------------------------------------------------

// The choices as the user sees them, matching the bot's /print command so the
// two front ends offer the same menu.
//
// Two of these are not real lipgloss styles. "label" and "label_qr" are
// resolved to their 1-line or 2-line variant by resolveStyle() below, based on
// whether line 2 was filled in -- lipgloss itself only knows the resolved
// names. classic_barcode is deliberately absent: it exists for /render, not
// for the label printer.
struct Style {
    const char* value;
    const char* label;
    bool needsSku;
    bool needsLine1;

    // Whether line 2 does anything for this style. False greys the field out
    // rather than silently ignoring what gets typed there.
    bool usesLine2;
};

constexpr Style kStyles[] = {
    { "slim_barcode",    "Barcode",                true,  false, false },
    { "label_barcode",   "Label w/ Barcode",       true,  true,  false },
    { "label_qr",        "Label w/ QR Code",       true,  true,  true  },
    { "label",           "Label",                  false, true,  true  },
    { "cable_label",     "Cable Label",            false, true,  true  },
    { "cable_label_sku", "Cable Label w/ SKU",     true,  true,  true  },
    { "cable_label_qr",  "Cable Label w/ QR Code", true,  true,  false },
};

constexpr int kStyleCount = static_cast<int>(sizeof(kStyles) / sizeof(kStyles[0]));

bool blank(const char* text) {
    for (const char* c = text; *c; ++c) {
        if (*c != ' ' && *c != '\t' && *c != '\r' && *c != '\n') {
            return false;
        }
    }

    return true;
}

// Mirrors illusion_helpers.clean_sku: anything six characters or shorter is
// padded into the full EER-nnnnnn form, so typing 421 finds EER-000421.
std::string cleanSku(const std::string& sku) {
    if (sku.empty() || sku.size() > 6) {
        return sku;
    }

    return "EER-" + std::string(6 - sku.size(), '0') + sku;
}

// Turns a picked style plus the filled-in fields into what lipgloss actually
// understands. Mirrors print_niimbot in illusion-bot's __main__.py; if the
// pairings change there they have to change here.
//
// The cable styles are the odd ones out: they always render two rows, so an
// empty second line repeats the first rather than leaving a blank half.
std::string resolveStyle(
    const Style& style,
    const std::string& line1,
    std::string& line2)
{
    const std::string name = style.value;

    if (line2.empty() && (name == "cable_label" || name == "cable_label_sku")) {
        line2 = line1;
    }

    if (name == "label") {
        return line2.empty() ? "label_1_line" : "label_2_line";
    }

    if (name == "label_qr") {
        return line2.empty() ? "label_1_line_qr" : "label_2_line_qr";
    }

    return name;
}

// Why the Print button is disabled, or nullptr when it is not. lipgloss checks
// this too and answers 200 with a refusal message, but saying it here means
// the round trip is not needed to find out.
const char* printBlocker(const Style& style) {
    if (style.needsSku && blank(g_print.sku)) {
        return "This style needs a SKU.";
    }

    if (style.needsLine1 && blank(g_print.line1)) {
        return "This style needs line 1.";
    }

    return nullptr;
}

constexpr ImVec4 kGood { 0.35f, 0.80f, 0.45f, 1.0f };
constexpr ImVec4 kWarn { 0.90f, 0.65f, 0.30f, 1.0f };
constexpr ImVec4 kBad  { 0.88f, 0.36f, 0.36f, 1.0f };

// The state of lipgloss in 3 words
struct Status {
    ImVec4 colour;
    const char* label;
};

Status statusOf(const lipgloss::Snapshot& snapshot) {
    if (!snapshot.reachable) {
        return { kBad, "Unreachable" };
    }

    if (snapshot.unauthorized) {
        return { kWarn, "Token rejected" };
    }

    if (snapshot.paused) {
        return { kWarn, "Queue paused" };
    }

    return { kGood, "Connected" };
}

// The indicator and the refresh button, drawn inside the menu bar.
// SameLine isnt required because BeginMenuBar already handles that
void drawStatusBar(const lipgloss::Snapshot& snapshot, lipgloss::Client& client) {
    const Status status = statusOf(snapshot);
    const ImGuiStyle& style = ImGui::GetStyle();

    constexpr const char* kDot = "\xe2\x97\x8f";  // U+25CF BLACK CIRCLE

    // Right alligned
    // Calculated based off the width of whats about to be drawn, because imgui
    // doesn't have a way to natively handle this 
    // 
    // SmallButton's width is its label plus FramePadding.x on each side, it
    // zeroes only the vertical padding. The two ItemSpacing gaps are the ones
    // the horizontal layout will insert between the three items.
    const float width =
        ImGui::CalcTextSize(kDot).x +
        ImGui::CalcTextSize(status.label).x +
        ImGui::CalcTextSize("Refresh").x + style.FramePadding.x * 2.0f +
        style.ItemSpacing.x * 2.0f;

    const float avail = ImGui::GetContentRegionAvail().x;

    // Offset from where the cursor already is rather than an absolute X
    if (avail > width) {
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + avail - width);
    }

    ImGui::TextColored(status.colour, "%s", kDot);
    bool hovered = ImGui::IsItemHovered();

    ImGui::TextUnformatted(status.label);
    hovered = hovered || ImGui::IsItemHovered();

    // Checked on both halves so hovering the dot works as well as the word.
    if (hovered && !snapshot.error.empty()) {
        ImGui::SetTooltip("%s", snapshot.error.c_str());
    }

    if (ImGui::Button("Refresh")) {
        client.refresh();
    }
}

// A two-column key/value block: dimmed label on the left, value on the right,
// columns sized to their content. Every block on the About page is built from
// these two calls so they read as one kind of thing rather than as several.
bool beginInfoTable(const char* id) {
    return ImGui::BeginTable(id, 2, ImGuiTableFlags_SizingFixedFit);
}

void infoRow(const char* key, const char* value) {
    ImGui::TableNextRow();
    ImGui::TableNextColumn();
    ImGui::TextDisabled("%s", key);
    ImGui::TableNextColumn();
    ImGui::TextUnformatted(value);
}

// What this binary is. Fixed for the life of the process.
void drawBuildInfo(SDL_GPUDevice* gpuDevice) {
    if (!beginInfoTable("build")) {
        return;
    }

    infoRow("Version", SGUMI_VERSION);
    infoRow("Built", EER_BUILD_DATE);

    // Theoretically it should be impossible for this to be needed, because
    // a gpu is needed to even render this, but its good practice to have
    // a fallback, even if its realistically useless.
    const char* driver = SDL_GetGPUDeviceDriver(gpuDevice);
    infoRow("Renderer", driver ? driver : "unknown");

    ImGui::EndTable();
}

// lipgloss's own identity and health, from GET /health.
void drawServiceInfo(const lipgloss::Snapshot& snapshot) {
    if (!snapshot.reachable) {
        ImGui::TextDisabled("Not connected.");
        return;
    }

    if (!beginInfoTable("health")) {
        return;
    }

    infoRow("Version", snapshot.version.c_str());
    infoRow("Model", snapshot.model.c_str());
    infoRow("Printer port", snapshot.printerPort.c_str());
    infoRow("Uptime", formatUptime(snapshot.uptimeMs).c_str());

    ImGui::EndTable();
}

// claws, which only matters here for whether a SKU lookup would work. It gets
// no place in the status bar for that reason -- printing works fine without
// it, you just have to type the label text yourself.
void drawClawsInfo(const claws::Snapshot& snapshot) {
    if (!snapshot.reachable) {
        ImGui::TextDisabled("Not connected.");

        if (!snapshot.error.empty()) {
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextDisabled("%s", snapshot.error.c_str());
            ImGui::PopTextWrapPos();
        }

        return;
    }

    if (!beginInfoTable("clawsHealth")) {
        return;
    }

    infoRow("Version", snapshot.version.c_str());
    infoRow("Uptime", formatUptime(snapshot.uptimeMs).c_str());
    infoRow("Token", snapshot.unauthorized ? "Rejected" : "OK");

    ImGui::EndTable();
}

// ---------------------------------------------------------------------------
// Form layout
//
// ImGui puts a widget's label to its *right*, which reads badly in a form.
// These put the label in a fixed left column instead, and are written so a row
// can hold two label+field pairs -- the offset is measured from where each
// label starts, not from the edge of the window, so the second pair on a row
// lines up the same way the first does.
// ---------------------------------------------------------------------------

constexpr float kLabelWidth = 76.0f;
constexpr float kFieldMaxWidth = 220.0f;

// One pair filling the row.
float fieldWidth() {
    const float avail = ImGui::GetContentRegionAvail().x - kLabelWidth;
    return std::min(std::max(avail, 80.0f), kFieldMaxWidth);
}

// One of two pairs sharing the row. Falls back to something usable rather than
// going negative when the window is dragged narrow.
float halfFieldWidth() {
    const float avail = ImGui::GetContentRegionAvail().x;
    const float perPair = (avail - ImGui::GetStyle().ItemSpacing.x) * 0.5f;
    return std::min(std::max(perPair - kLabelWidth, 70.0f), kFieldMaxWidth);
}

// AlignTextToFramePadding so the label sits on the widget's baseline rather
// than riding up against the top of its frame. The cursor is then put a fixed
// distance past where *this* label began, which is what lets a second pair on
// the same row align like the first.
void fieldLabel(const char* text) {
    const float x = ImGui::GetCursorPosX();

    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text);
    ImGui::SameLine();
    ImGui::SetCursorPosX(x + kLabelWidth);
}

// A range endpoint takes plain numbers, but these boxes take SKUs, because a
// SKU is what is printed on the bin and what a scanner types. Accepts either
// form: "EER-000421" and "421" both give 421.
bool skuNumber(const char* text, int& out) {
    std::string value = text;

    const size_t dash = value.find_last_of('-');

    if (dash != std::string::npos) {
        value = value.substr(dash + 1);
    }

    if (value.empty() ||
        value.find_first_not_of("0123456789") != std::string::npos) {
        return false;
    }

    // strtol rather than stoi: no exception to catch on a number too long to
    // fit, which someone leaning on a keypad will produce eventually.
    errno = 0;
    const long parsed = std::strtol(value.c_str(), nullptr, 10);

    if (errno != 0 || parsed < 0 || parsed > 999999) {
        return false;
    }

    out = static_cast<int>(parsed);
    return true;
}

// What claws said about the SKU in the box, and the fill of line 1.
//
// The fill is what the bot calls get_text_from_sku: the item's name becomes
// the label's first line. Applied once per lookup rather than every frame, so
// that the toggle being on does not re-clobber the field continuously.
void drawLookupResult(const claws::Lookup& lookup) {
    switch (lookup.state) {
    case claws::Lookup::State::Idle:
        return;

    case claws::Lookup::State::Pending:
        ImGui::TextDisabled("Looking up %s...", lookup.sku.c_str());
        return;

    case claws::Lookup::State::NotFound:
        ImGui::TextColored(kWarn, "No item with SKU %s.", lookup.sku.c_str());
        return;

    case claws::Lookup::State::Failed:
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(kBad, "%s", lookup.error.c_str());
        ImGui::PopTextWrapPos();
        return;

    case claws::Lookup::State::Found:
        break;
    }

    if (g_print.filledFromSku != lookup.sku) {
        std::snprintf(g_print.line1, sizeof(g_print.line1), "%s",
                      lookup.item.name.c_str());
        g_print.filledFromSku = lookup.sku;
    }

    ImGui::PushTextWrapPos(0.0f);
    ImGui::TextColored(kGood, "%s", lookup.item.name.c_str());
    ImGui::PopTextWrapPos();

    // Stock is worth a glance while printing a label for a bin, but a KANBAN
    // item genuinely has no count -- showing 0 for one would read as empty.
    std::string detail;

    if (lookup.item.trackingMode != "KANBAN") {
        detail = std::to_string(lookup.item.quantityOnHand) + " on hand";
    }

    if (!lookup.item.location.empty()) {
        if (!detail.empty()) {
            detail += ", ";
        }

        detail += "at " + lookup.item.location;
    }

    if (!detail.empty()) {
        ImGui::TextDisabled("%s", detail.c_str());
    }

    if (lookup.item.low) {
        ImGui::SameLine();
        ImGui::TextColored(kWarn, "LOW");
    }
}

// The form as lipgloss wants it. Used for both Print and Preview, so the
// preview is guaranteed to render the label the print would.
lipgloss::PrintRequest buildRequest(const Style& style) {
    lipgloss::PrintRequest request;

    std::string line2 = g_print.line2;
    request.style = resolveStyle(style, g_print.line1, line2);

    // Only when the style actually puts it on the label. The box can hold a
    // SKU for a style that does not use one -- that is how claws is asked for
    // the item name -- and sending it anyway would change what gets rendered.
    request.sku = style.needsSku ? cleanSku(g_print.sku) : "";

    request.line1 = style.needsLine1 ? g_print.line1 : "";
    request.line2 = style.usesLine2 ? line2 : "";
    request.copies = g_print.copies;

    return request;
}

// Everything the preview depends on, flattened. Compared against what the
// showing preview was made from, to notice when it has gone stale -- copies is
// left out on purpose, since printing three of a label does not change how it
// looks.
std::string requestSignature(const Style& style) {
    if (g_print.rangeMode) {
        // A range's preview is only ever its first label, so that is the only
        // field that changes what is shown.
        return std::string("range\x1f") + g_print.rangeFrom;
    }

    const lipgloss::PrintRequest request = buildRequest(style);

    return request.style + '\x1f' + request.sku + '\x1f' +
           request.line1 + '\x1f' + request.line2;
}

// The label shown before anything has been previewed.
//
// A real render from lipgloss rather than something drawn here, so it shows
// exactly what the app produces and the QR code actually scans. The SKU is not
// arbitrary.
constexpr const char* kExampleStyle = "label_1_line_qr";
constexpr const char* kExampleSku = "EER-120607";
constexpr const char* kExampleLine1 = "Example Text!";

void submitExamplePreview(lipgloss::Client& client) {
    lipgloss::PrintRequest request;
    request.style = kExampleStyle;
    request.sku = kExampleSku;
    request.line1 = kExampleLine1;

    client.submitPreview(request);

    g_print.previewIsExample = true;
    g_print.exampleRequested = true;

    // Left empty so the first real refresh cannot mistake the example for a
    // preview of the form and skip itself.
    g_print.previewOf.clear();
}

// Asks for whatever the form currently describes, and records what it was
// asked for. Both modes go through here so every caller stays consistent.
void submitPreview(lipgloss::Client& client, const Style& style) {
    if (g_print.rangeMode) {
        // Only the first label of the run: they differ solely by SKU, so one
        // is representative and asking for all of them would be silly.
        lipgloss::PrintRequest request;
        request.style = "slim_barcode";
        request.sku = cleanSku(g_print.rangeFrom);
        client.submitPreview(request);
    } else {
        client.submitPreview(buildRequest(style));
    }

    g_print.previewOf = requestSignature(style);
    g_print.previewIsExample = false;
}

// The preview's texture and the serial it was uploaded from.
//
// Owned by runSgumi, deliberately. This started out as a function-local static
// and segfaulted on every exit: a static is destroyed by __cxa_finalize_ranges
// during exit(), which is long after SDL_DestroyGPUDevice has run, and
// releasing a texture against a destroyed device dereferences null. Holding it
// in the frame loop's scope means it can be reset while the device is still
// alive -- see the shutdown sequence.
struct PreviewPanel {
    image::Texture texture;
    unsigned long long uploaded = 0;
};

// Sits above the form, always present, so the label being described is the
// first thing on screen rather than something found by scrolling.
//
// Capped rather than filling the row: lipgloss returns the label already
// scaled up for a screen, and letting that set the window's width would make
// the whole app as wide as a 960px preview for no benefit.
constexpr float kPreviewMaxWidth = 420.0f;

// A label-shaped blank, drawn rather than fetched.
//
// Gives the panel something to hold before anything has been asked of
// lipgloss, and -- because it takes the width and proportions a real preview
// would -- stops the whole form jumping down the screen the first time one
// arrives.
void drawPlaceholderLabel(float width) {
    // The Niimbot label's own proportions, 320x96. lipgloss adds a one pixel
    // frame on each side, which at this size is not worth reproducing.
    const ImVec2 size(width, width * (96.0f / 320.0f));
    const ImVec2 pos = ImGui::GetCursorScreenPos();
    const ImVec2 end(pos.x + size.x, pos.y + size.y);

    ImDrawList* draw = ImGui::GetWindowDrawList();

    // Paper, with the same frame lipgloss draws round its previews, so this
    // reads as a blank label rather than as a failed image.
    draw->AddRectFilled(pos, end, IM_COL32(237, 234, 228, 255));
    draw->AddRect(pos, end, IM_COL32(20, 22, 27, 255));

    const char* text = "No preview yet";
    const ImVec2 textSize = ImGui::CalcTextSize(text);

    draw->AddText(
        ImVec2(pos.x + (size.x - textSize.x) * 0.5f,
               pos.y + (size.y - textSize.y) * 0.5f),
        IM_COL32(120, 125, 135, 255),
        text);

    // The drawing above is free-floating, so the layout still has to be told
    // how much room it took.
    ImGui::Dummy(size);
}

void drawPreview(
    const lipgloss::PreviewResult& preview,
    const Style& style,
    SDL_GPUDevice* device,
    PreviewPanel& panel)
{
    image::Texture& texture = panel.texture;

    // Uploaded only when the bytes actually change, rather than decoding the
    // same PNG every frame.
    if (preview.serial != panel.uploaded) {
        panel.uploaded = preview.serial;

        if (preview.png.empty()) {
            texture.reset();
        } else {
            texture.load(device, preview.png.data(), preview.png.size());
        }
    }

    // The same width either way, so swapping a blank for a real label moves
    // nothing else on screen.
    const float limit =
        std::min(ImGui::GetContentRegionAvail().x, kPreviewMaxWidth);

    if (texture.valid()) {
        const float natural = static_cast<float>(texture.width());

        // Only ever shrinks: the source is already blown up for a screen, and
        // enlarging it further would just blur it. Aspect ratio preserved so a
        // barcode is never stretched into something that would not scan.
        const float scale = natural > limit ? limit / natural : 1.0f;

        ImGui::Image(
            texture.id(),
            ImVec2(natural * scale,
                   static_cast<float>(texture.height()) * scale));
    } else {
        drawPlaceholderLabel(limit);
    }

    if (preview.state == lipgloss::PreviewResult::State::Pending) {
        ImGui::TextDisabled("Rendering...");
    } else if (!preview.error.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextColored(kBad, "%s", preview.error.c_str());
        ImGui::PopTextWrapPos();
    } else if (g_print.previewIsExample) {
        ImGui::TextDisabled("Example label.");
    } else if (texture.valid() &&
               g_print.previewOf != requestSignature(style)) {
        // A picture of fields that have since been edited, which is worth
        // saying rather than letting someone print something they did not look
        // at. Rare now that the refresh is automatic -- it survives for the
        // cases the refresh declines, like lipgloss being unreachable.
        ImGui::TextColored(kWarn, "Fields changed since this preview.");
    }
}

void drawPrint(
    lipgloss::Client& lipglossClient,
    claws::Client& clawsClient,
    const lipgloss::Snapshot& snapshot)
{
    // Set by anything that changes how the label looks. Checked once at the
    // end, where the request is known to be valid, so the preview keeps up
    // without the button being pressed.
    //
    // Deliberately not per keystroke: IsItemDeactivatedAfterEdit fires when a
    // field is left after being changed, which is one request per field rather
    // than one per character.
    bool changed = false;

    // ---- Style, and quantity beside it when there is one ----
    fieldLabel("Style");
    ImGui::SetNextItemWidth(
        g_print.rangeMode ? fieldWidth() : halfFieldWidth());

    if (ImGui::BeginCombo("##style", kStyles[g_print.styleIndex].label)) {
        for (int i = 0; i < kStyleCount; ++i) {
            const bool selected = i == g_print.styleIndex;

            if (ImGui::Selectable(kStyles[i].label, selected)) {
                g_print.styleIndex = i;
                changed = true;
            }

            if (selected) {
                ImGui::SetItemDefaultFocus();
            }
        }

        ImGui::EndCombo();
    }

    // Bound *after* the combo, not before it. A reference taken first would
    // still point at the previous style once the combo had changed the index,
    // so the automatic refresh would re-render the old label, find the
    // signature unchanged, and decide there was nothing to do -- which is
    // exactly why changing the style used to leave the preview alone.
    const Style& style = kStyles[g_print.styleIndex];

    if (!g_print.rangeMode) {
        ImGui::SameLine();
        fieldLabel("Quantity");
        ImGui::SetNextItemWidth(halfFieldWidth());
        ImGui::InputInt("##copies", &g_print.copies);
        g_print.copies = std::clamp(g_print.copies, 1, lipgloss::kMaxCopies);
    }

    if (g_print.rangeMode) {
        // ---- A range of SKUs, one label each ----
        fieldLabel("From SKU");
        ImGui::SetNextItemWidth(fieldWidth());
        ImGui::InputText("##rangeFrom", g_print.rangeFrom,
                         sizeof(g_print.rangeFrom));
        changed = changed || ImGui::IsItemDeactivatedAfterEdit();

        fieldLabel("To SKU");
        ImGui::SetNextItemWidth(fieldWidth());
        ImGui::InputText("##rangeTo", g_print.rangeTo, sizeof(g_print.rangeTo));

        // Not folded into `changed`: the last SKU does not alter the first
        // label, which is all the preview ever shows for a range.
    } else {
        // ---- SKU, with the claws toggle beside it ----
        //
        // The box is enabled whenever the SKU is of use to anything: either the
        // style puts it on the label, or claws is being asked to name the item.
        // A "Label" carries no SKU but still needs one typed here to look the
        // name up, which is the whole point of the toggle.
        const bool skuWanted = style.needsSku || g_print.useSkuName;

        fieldLabel("SKU");
        ImGui::BeginDisabled(!skuWanted);
        ImGui::SetNextItemWidth(halfFieldWidth());

        // EnterReturnsTrue for the barcode scanner, which types a SKU and
        // presses enter. IsItemDeactivatedAfterEdit catches the mouse case,
        // someone typing and then clicking away. Together they mean "the SKU
        // is finished", which is when a lookup is worth making; firing per
        // keystroke would ask claws about every prefix.
        const bool entered = ImGui::InputText(
            "##sku", g_print.sku, sizeof(g_print.sku),
            ImGuiInputTextFlags_EnterReturnsTrue);

        const bool committed = entered || ImGui::IsItemDeactivatedAfterEdit();

        // Only matters for styles that put the SKU on the label. When claws is
        // filling line 1 this usually fires twice -- once for the new SKU, once
        // when the name lands -- but the client holds a single pending preview,
        // so the two collapse into one request more often than not.
        changed = changed || committed;

        ImGui::EndDisabled();

        ImGui::SameLine();

        // Never disabled, even for a style with no SKU on it: switching this on
        // is what makes the SKU box above usable in the first place.
        const bool toggled = ImGui::Checkbox("From claws", &g_print.useSkuName);

        if (ImGui::IsItemHovered()) {
            ImGui::SetTooltip(
                "Fill line 1 with the item's name from claws.");
        }

        if (g_print.useSkuName) {
            if ((toggled || committed) && !blank(g_print.sku)) {
                g_print.filledFromSku.clear();
                clawsClient.lookup(cleanSku(g_print.sku));
            }
        } else if (toggled) {
            // Switched off: line 1 goes back to being the user's, keeping
            // whatever the last lookup put there rather than blanking it.
            clawsClient.clearLookup();
            g_print.filledFromSku.clear();
        }

        // ---- The two text lines ----
        //
        // Read after the lookup above so a freshly asked SKU shows "Looking
        // up" this frame rather than next.
        const claws::Lookup lookup = clawsClient.lookupResult();

        // Locked only while claws is actually supplying it. An editable field
        // something else rewrites is a trap, but so is a locked empty one: with
        // the toggle on and a SKU claws has never heard of, line 1 would
        // otherwise be both empty and uneditable, leaving Print blocked on
        // "needs line 1" with no way out.
        const bool line1Derived =
            g_print.useSkuName && lookup.state == claws::Lookup::State::Found;

        fieldLabel("Line 1");
        ImGui::BeginDisabled(!style.needsLine1 || line1Derived);
        ImGui::SetNextItemWidth(halfFieldWidth());
        ImGui::InputText("##line1", g_print.line1, sizeof(g_print.line1));
        changed = changed || ImGui::IsItemDeactivatedAfterEdit();
        ImGui::EndDisabled();

        ImGui::SameLine();

        fieldLabel("Line 2");
        ImGui::BeginDisabled(!style.usesLine2);
        ImGui::SetNextItemWidth(halfFieldWidth());
        ImGui::InputText("##line2", g_print.line2, sizeof(g_print.line2));
        changed = changed || ImGui::IsItemDeactivatedAfterEdit();
        ImGui::EndDisabled();

        if (g_print.useSkuName) {
            // A lookup landing rewrites line 1, and that is a change to the
            // label as much as typing one would be -- without this the preview
            // would still show whatever line 1 held before claws answered.
            const std::string filledBefore = g_print.filledFromSku;
            drawLookupResult(lookup);
            changed = changed || g_print.filledFromSku != filledBefore;
        }
    }

    // ---- The mode switch ----
    if (ImGui::Checkbox("Range print", &g_print.rangeMode)) {
        // The preview on screen describes the other mode's fields, so it is
        // dropped back to the blank rather than left showing the wrong label.
        lipglossClient.clearPreview();
        g_print.previewOf.clear();
        changed = true;
    }

    if (ImGui::IsItemHovered()) {
        ImGui::SetTooltip("Print one label per SKU across a range.");
    }

    ImGui::Spacing();

    // ---- What can be pressed, and why not ----
    const lipgloss::ActionResult action = lipglossClient.actionResult();
    const bool pending = action.state == lipgloss::ActionResult::State::Pending;

    int rangeFrom = 0;
    int rangeTo = 0;
    const char* blocker = nullptr;

    if (g_print.rangeMode) {
        if (!skuNumber(g_print.rangeFrom, rangeFrom)) {
            blocker = "Enter a starting SKU.";
        } else if (!skuNumber(g_print.rangeTo, rangeTo)) {
            blocker = "Enter an ending SKU.";
        } else if (rangeTo < rangeFrom) {
            blocker = "The first SKU is higher than the last.";
        }
    } else {
        blocker = printBlocker(style);
    }

    const bool disabled = pending || blocker != nullptr || !snapshot.reachable;

    // Automatic refresh. Gated on the request being valid and on the fields
    // having actually moved since the showing preview was made -- leaving a
    // box without changing anything asks lipgloss for nothing.
    if (changed &&
        blocker == nullptr &&
        snapshot.reachable &&
        requestSignature(style) != g_print.previewOf) {
        submitPreview(lipglossClient, style);
    }

    ImGui::BeginDisabled(disabled);

    if (ImGui::Button("Print")) {
        if (g_print.rangeMode) {
            lipglossClient.submitBarcodes(rangeFrom, rangeTo);
        } else {
            lipglossClient.submitPrint(buildRequest(style));
        }
    }

    ImGui::EndDisabled();

    ImGui::SameLine();

    if (pending) {
        ImGui::TextDisabled("Sending...");
    } else if (!snapshot.reachable) {
        ImGui::TextColored(kBad, "lipgloss is unreachable.");
    } else if (blocker) {
        ImGui::TextDisabled("%s", blocker);
    } else if (g_print.rangeMode) {
        const int total = rangeTo - rangeFrom + 1;
        ImGui::TextDisabled("%d label%s.", total, total == 1 ? "" : "s");
    } else {
        switch (action.state) {
        case lipgloss::ActionResult::State::Ok:
            if (action.kind == lipgloss::ActionResult::Kind::Resume) {
                // Reported under the queue, not here. Without this the resume
                // would land in the print status line as "Job -1 queued".
                break;
            }

            // A job accepted onto a paused queue is not printing, and saying
            // "queued" without that would be misleading.
            if (action.queuePaused) {
                ImGui::TextColored(kWarn, "Job %lld queued, queue is paused.",
                                   action.jobId);
            } else {
                ImGui::TextColored(kGood, "Job %lld queued.", action.jobId);
            }
            break;

        case lipgloss::ActionResult::State::Failed:
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextColored(kBad, "%s", action.message.c_str());
            ImGui::PopTextWrapPos();
            break;

        default:
            break;
        }
    }

    if (g_print.rangeMode) {
        // The style selector is above because the layout is meant to outlive
        // this limitation, but POST /print/barcodes takes no style and renders
        // slim_barcode itself. Said plainly rather than letting the selector
        // imply a choice that does not reach the wire yet.
        ImGui::TextDisabled(
            "Range printing always uses Barcode for now; the style above is "
            "not sent.");

        if (action.state == lipgloss::ActionResult::State::Ok) {
            ImGui::TextColored(kGood, "Job %lld queued.", action.jobId);
        } else if (action.state == lipgloss::ActionResult::State::Failed) {
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextColored(kBad, "%s", action.message.c_str());
            ImGui::PopTextWrapPos();
        }
    }
}

void drawQueue(const lipgloss::Snapshot& snapshot, lipgloss::Client& client) {
    if (!snapshot.reachable || snapshot.unauthorized) {
        ImGui::TextDisabled("Queue unavailable.");

        // The indicator in the bar has room for three words, so the reason goes here
        if (!snapshot.error.empty()) {
            ImGui::Spacing();
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextColored(snapshot.unauthorized ? kWarn : kBad, "%s",
                               snapshot.error.c_str());
            ImGui::PopTextWrapPos();
        }

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

    // Only while paused. lipgloss stops the queue when the printer needs
    // attention -- out of labels, lid open, unplugged -- and nothing starts
    // printing again until someone says the problem is dealt with. The reason
    // is in the description above, so this is just the acknowledgement.
    if (snapshot.paused) {
        const lipgloss::ActionResult action = client.actionResult();
        const bool pending =
            action.state == lipgloss::ActionResult::State::Pending;

        ImGui::Spacing();
        ImGui::BeginDisabled(pending);

        if (ImGui::Button("Resume queue")) {
            client.submitResume();
        }

        ImGui::EndDisabled();

        if (pending) {
            ImGui::SameLine();
            ImGui::TextDisabled("Resuming...");
        } else if (action.kind == lipgloss::ActionResult::Kind::Resume &&
                   action.state != lipgloss::ActionResult::State::Idle) {
            // lipgloss answers 200 even when it could not resume -- "still
            // unable to print, the queue is staying paused" is a successful
            // request with an unsuccessful outcome. Its own wording is the
            // only thing that tells the two apart, so it is shown verbatim,
            // and the colour comes from whether the queue is actually still
            // paused rather than from the HTTP status.
            ImGui::Spacing();
            ImGui::PushTextWrapPos(0.0f);
            ImGui::TextColored(kWarn, "%s", action.message.c_str());
            ImGui::PopTextWrapPos();
        }
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

    // Useful on Linux desktops, this is what DEs match against the
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

    // Narrow and tall rather than square
    SDL_Window* window = SDL_CreateWindow("SGUMI", 620, 820, windowFlags);

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

    claws::Client clawsClient;
    clawsClient.setEndpoint(g_config.clawsUrl, g_config.clawsToken);
    clawsClient.start();

    PreviewPanel previewPanel;

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

        // The main window fills the OS window and is not movable
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

                    ImGui::EndMenu();
                }

                drawStatusBar(snapshot, client);

                ImGui::EndMenuBar();
            }

            const lipgloss::PreviewResult previewResult = client.previewResult();

            // Nothing has ever been previewed, so show a real label rather
            // than an empty frame.
            if (!g_print.exampleRequested &&
                previewResult.state == lipgloss::PreviewResult::State::Idle &&
                snapshot.reachable) {
                submitExamplePreview(client);
            }

            // The preview goes first: the label is what this screen is about,
            // and the fields below are how it gets changed.
            drawPreview(previewResult,
                        kStyles[g_print.styleIndex],
                        gpuDevice,
                        previewPanel);

            ImGui::Spacing();
            ImGui::SeparatorText("Print");
            drawPrint(client, clawsClient, snapshot);

            ImGui::Spacing();
            ImGui::SeparatorText("Print queue");
            drawQueue(snapshot, client);
        }

        ImGui::End();

        if (showSettings) {
            ImGui::SetNextWindowSize(ImVec2(750, 400), ImGuiCond_FirstUseEver);

            if (ImGui::Begin("Settings", &showSettings)) {
                // Shared by both service tabs rather than duplicated into
                // each. Apply pushes both endpoints, because the config file
                // holds both and saving half of it would be a strange thing
                // for a button labelled "save" to do.
                auto drawApplyRow = [&] {
                    ImGui::Spacing();

                    if (ImGui::Button("Apply and save")) {
                        // Applied to the live clients either way: a token that
                        // works is worth having for this session even if the
                        // disk write failed.
                        client.setEndpoint(g_config.lipglossUrl,
                                           g_config.lipglossToken);
                        clawsClient.setEndpoint(g_config.clawsUrl,
                                                g_config.clawsToken);

                        configStatus = saveConfigToFile(configPath, g_config)
                                        ? "Saved to " + configPath.string()
                                        : "Failed to write " + configPath.string();
                    }

                    ImGui::SameLine();

                    if (ImGui::Button("Apply without saving")) {
                        client.setEndpoint(g_config.lipglossUrl,
                                           g_config.lipglossToken);
                        clawsClient.setEndpoint(g_config.clawsUrl,
                                                g_config.clawsToken);
                        configStatus = "Applied for this session only.";
                    }

                    if (!configStatus.empty()) {
                        ImGui::Spacing();
                        ImGui::PushTextWrapPos(0.0f);
                        ImGui::TextDisabled("%s", configStatus.c_str());
                        ImGui::PopTextWrapPos();
                    }
                };

                if (ImGui::BeginTabBar("Config Tabs")) {
                    if (ImGui::BeginTabItem("Lipgloss")) {
                        ImGui::TextDisabled("Must match lipgloss.yaml on the print server.");
                        ImGui::Spacing();

                        ImGui::InputText("lipgloss URL", g_config.lipglossUrl,
                                        sizeof(g_config.lipglossUrl));
                        ImGui::InputText("Token", g_config.lipglossToken,
                                        sizeof(g_config.lipglossToken),
                                        ImGuiInputTextFlags_Password);

                        drawApplyRow();

                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("Claws")) {
                        ImGui::TextDisabled(
                            "Must match claws.yaml on the inventory host. "
                            "Only used to look a SKU up; SGUMI never writes to claws.");
                        ImGui::Spacing();

                        ImGui::InputText("claws URL", g_config.clawsUrl,
                                        sizeof(g_config.clawsUrl));
                        ImGui::InputText("Token", g_config.clawsToken,
                                        sizeof(g_config.clawsToken),
                                        ImGuiInputTextFlags_Password);

                        drawApplyRow();

                        ImGui::EndTabItem();
                    }

                    if (ImGui::BeginTabItem("About")) {
                        ImGui::TextUnformatted("Super Graphic Ultra Modern Interface");
                        ImGui::TextDisabled("illusion's frontend for lipgloss");

                        // Two versions, and they are not the same thing: this
                        // build, and whatever the service on the other end
                        // happens to be running. Kept apart so a mismatch
                        // between them is visible rather than confusing.
                        ImGui::SeparatorText("This build");
                        drawBuildInfo(gpuDevice);

                        ImGui::SeparatorText("lipgloss");
                        drawServiceInfo(snapshot);

                        ImGui::SeparatorText("claws");
                        drawClawsInfo(clawsClient.snapshot());

                        ImGui::Spacing();
                        ImGui::Separator();
                        ImGui::TextDisabled("Config: %s", configPath.string().c_str());

                        ImGui::EndTabItem();
                    }
                    ImGui::EndTabBar();
                }
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
    clawsClient.stop();

    SDL_WaitForGPUIdle(gpuDevice);

    // Before the device goes: SDL_ReleaseGPUTexture needs the device that made
    // the texture. Letting this run on its own at scope exit would put it
    // after SDL_DestroyGPUDevice below, which is a null dereference -- the
    // exact crash an earlier version of this shipped with.
    previewPanel.texture.reset();

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

    // Once, around everything. libcurl's global init is not thread-safe and
    // happens implicitly inside the first curl_easy_init() if nobody does it
    // first -- with two clients on two worker threads, that implicit init
    // would be a race. See Http.hpp.
    http::globalInit();

    const int result = runSgumi();

    http::globalCleanup();
    return result;
}
