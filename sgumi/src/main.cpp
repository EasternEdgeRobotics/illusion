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

    // The barcode range printer, which shares nothing with the fields above.
    int rangeLower = 1;
    int rangeUpper = 1;
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
// ImGui puts a widget's label to its *right*, which reads badly in a form and
// spends width on the side where a narrow screen has none. These two put the
// label in a fixed left column instead, and cap the field so the whole row
// fits a phone: 96 + 240 plus window padding lands inside a 390pt portrait
// viewport, which is what the eventual iOS port has to live in.
// ---------------------------------------------------------------------------

constexpr float kLabelWidth = 96.0f;
constexpr float kFieldMaxWidth = 240.0f;

// Shrinks below the cap when the window is narrower, never below something
// still usable -- a field clipped to nothing is worse than one that overflows.
float fieldWidth() {
    const float avail = ImGui::GetContentRegionAvail().x - kLabelWidth;
    return std::min(std::max(avail, 90.0f), kFieldMaxWidth);
}

// AlignTextToFramePadding so the label sits on the widget's baseline rather
// than riding up against the top of its frame.
void fieldLabel(const char* text) {
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text);
    ImGui::SameLine(kLabelWidth);
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

void drawPrint(
    lipgloss::Client& lipglossClient,
    claws::Client& clawsClient,
    const lipgloss::Snapshot& snapshot)
{
    const Style& style = kStyles[g_print.styleIndex];

    fieldLabel("Style");
    ImGui::SetNextItemWidth(fieldWidth());

    if (ImGui::BeginCombo("##style", style.label)) {
        for (int i = 0; i < kStyleCount; ++i) {
            const bool selected = i == g_print.styleIndex;

            if (ImGui::Selectable(kStyles[i].label, selected)) {
                g_print.styleIndex = i;
            }

            if (selected) {
                ImGui::SetItemDefaultFocus();
            }
        }

        ImGui::EndCombo();
    }

    // SKU. Disabled entirely for styles that put no SKU on the label, so the
    // box cannot be filled in and then silently ignored.
    ImGui::BeginDisabled(!style.needsSku);

    fieldLabel("SKU");
    ImGui::SetNextItemWidth(fieldWidth());

    // EnterReturnsTrue for the barcode scanner, which types a SKU and presses
    // enter, that is the normal way a SKU reaches this box on the kiosk.
    // IsItemDeactivatedAfterEdit catches the mouse case, someone typing and
    // then clicking away. Together they mean "the SKU is finished", which is
    // when a lookup is worth making; firing per keystroke would ask claws
    // about every prefix.
    const bool entered = ImGui::InputText(
        "##sku", g_print.sku, sizeof(g_print.sku),
        ImGuiInputTextFlags_EnterReturnsTrue);

    const bool committed = entered || ImGui::IsItemDeactivatedAfterEdit();

    // The toggle, indented to line up under the fields rather than under the
    // labels. Mirrors the bot's get_text_from_sku flag: while it is on, line 1
    // is claws's to fill and not the user's to type.
    ImGui::Indent(kLabelWidth);

    const bool toggled =
        ImGui::Checkbox("Get line 1 from SKU", &g_print.useSkuName);

    ImGui::Unindent(kLabelWidth);

    ImGui::EndDisabled();

    if (style.needsSku && g_print.useSkuName) {
        // Re-asked whenever the SKU is finished, and once when the toggle goes
        // on, so the name always describes the SKU currently in the box.
        const bool askNow = toggled || committed;

        if (askNow && !blank(g_print.sku)) {
            g_print.filledFromSku.clear();
            clawsClient.lookup(cleanSku(g_print.sku));
        }
    } else if (toggled && !g_print.useSkuName) {
        // Switched off: line 1 goes back to being the user's, keeping whatever
        // the last lookup put there rather than blanking it.
        clawsClient.clearLookup();
        g_print.filledFromSku.clear();
    }

    // Read after the lookup above so a freshly asked SKU shows "Looking up"
    // this frame rather than next.
    const claws::Lookup lookup = clawsClient.lookupResult();

    if (style.needsSku && g_print.useSkuName) {
        drawLookupResult(lookup);
    }

    // Line 1 is locked only while claws is actually supplying it, an
    // editable field that something else rewrites is a trap, but so is a
    // locked empty one.
    //
    // The state test is what stops the dead end: with the toggle on and a SKU
    // claws has never heard of, line 1 would otherwise be both empty and
    // uneditable, leaving Print permanently blocked on "needs line 1" with no
    // way out except noticing the toggle. A miss or an error hands the field
    // back so the text can just be typed.
    const bool line1Derived = style.needsSku && g_print.useSkuName &&
                              lookup.state == claws::Lookup::State::Found;

    fieldLabel("Line 1");
    ImGui::SetNextItemWidth(fieldWidth());
    ImGui::BeginDisabled(!style.needsLine1 || line1Derived);
    ImGui::InputText("##line1", g_print.line1, sizeof(g_print.line1));
    ImGui::EndDisabled();

    fieldLabel("Line 2");
    ImGui::SetNextItemWidth(fieldWidth());
    ImGui::BeginDisabled(!style.usesLine2);
    ImGui::InputText("##line2", g_print.line2, sizeof(g_print.line2));
    ImGui::EndDisabled();

    // The styles that change shape based on line 2 are worth calling out,
    // otherwise picking "Label" and getting a two-line label looks like a bug.
    const std::string styleValue = style.value;

    if (styleValue == "label" || styleValue == "label_qr") {
        ImGui::Indent(kLabelWidth);
        ImGui::TextDisabled(
            blank(g_print.line2)
                ? "One line. Fill line 2 for the two-line version."
                : "Two lines, because line 2 is filled in.");
        ImGui::Unindent(kLabelWidth);
    }

    fieldLabel("Copies");
    ImGui::SetNextItemWidth(fieldWidth());
    ImGui::InputInt("##copies", &g_print.copies);
    g_print.copies = std::clamp(g_print.copies, 1, lipgloss::kMaxCopies);

    ImGui::Spacing();

    const lipgloss::ActionResult action = lipglossClient.actionResult();
    const bool pending = action.state == lipgloss::ActionResult::State::Pending;
    const char* blocker = printBlocker(style);

    // Three separate reasons the button cannot be pressed, and the message
    // beside it says which. Disabled with no explanation is the thing to avoid.
    const bool disabled = pending || blocker != nullptr || !snapshot.reachable;

    ImGui::Indent(kLabelWidth);
    ImGui::BeginDisabled(disabled);

    if (ImGui::Button("Print")) {
        lipgloss::PrintRequest request;

        std::string line2 = g_print.line2;
        request.style = resolveStyle(style, g_print.line1, line2);
        request.sku = style.needsSku ? cleanSku(g_print.sku) : "";
        request.line1 = style.needsLine1 ? g_print.line1 : "";
        request.line2 = style.usesLine2 ? line2 : "";
        request.copies = g_print.copies;

        lipglossClient.submitPrint(request);
    }

    ImGui::EndDisabled();
    ImGui::Unindent(kLabelWidth);

    ImGui::SameLine();

    if (pending) {
        ImGui::TextDisabled("Sending...");
    } else if (!snapshot.reachable) {
        ImGui::TextColored(kBad, "lipgloss is unreachable.");
    } else if (blocker) {
        ImGui::TextDisabled("%s", blocker);
    } else {
        switch (action.state) {
        case lipgloss::ActionResult::State::Ok:
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
}

// Printing a run of blank SKU barcodes to stick on bins before anything is
// entered. Shares nothing with the form above, hence its own section.
void drawBarcodeRange(
    lipgloss::Client& lipglossClient,
    const lipgloss::Snapshot& snapshot)
{
    fieldLabel("From");
    ImGui::SetNextItemWidth(fieldWidth());
    ImGui::InputInt("##rangeLower", &g_print.rangeLower);

    fieldLabel("To");
    ImGui::SetNextItemWidth(fieldWidth());
    ImGui::InputInt("##rangeUpper", &g_print.rangeUpper);

    g_print.rangeLower = std::max(g_print.rangeLower, 0);
    g_print.rangeUpper = std::max(g_print.rangeUpper, 0);

    const int total = g_print.rangeUpper - g_print.rangeLower + 1;
    const bool inverted = g_print.rangeUpper < g_print.rangeLower;

    const lipgloss::ActionResult action = lipglossClient.actionResult();
    const bool pending = action.state == lipgloss::ActionResult::State::Pending;

    ImGui::Indent(kLabelWidth);
    ImGui::BeginDisabled(pending || inverted || !snapshot.reachable);

    if (ImGui::Button("Print range")) {
        lipglossClient.submitBarcodes(g_print.rangeLower, g_print.rangeUpper);
    }

    ImGui::EndDisabled();
    ImGui::Unindent(kLabelWidth);

    ImGui::SameLine();

    if (inverted) {
        ImGui::TextDisabled("From is higher than To.");
    } else {
        ImGui::TextDisabled("%d label%s, slim barcode.",
                            total, total == 1 ? "" : "s");
    }
}

void drawQueue(const lipgloss::Snapshot& snapshot) {
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

    claws::Client clawsClient;
    clawsClient.setEndpoint(g_config.clawsUrl, g_config.clawsToken);
    clawsClient.start();

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

                    ImGui::Separator();

                    if (ImGui::MenuItem("Quit")) {
                        done = true;
                    }

                    ImGui::EndMenu();
                }

                drawStatusBar(snapshot, client);

                ImGui::EndMenuBar();
            }

            ImGui::SeparatorText("Print");
            drawPrint(client, clawsClient, snapshot);

            ImGui::Spacing();

            // Collapsed by default: printing a run of blank barcodes is a
            // setup task, not something done during a normal shift.
            if (ImGui::CollapsingHeader("Barcode range")) {
                drawBarcodeRange(client, snapshot);
            }

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
