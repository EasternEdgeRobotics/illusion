#pragma once

#include <SDL3/SDL.h>
#include "imgui.h"

#include <cstddef>

// Decoding a PNG into a GPU texture, for the label preview.
//
// Ported from Software_2027's libs/eer_gfx/src/Image.cpp, with one difference
// that matters: over there textures are loaded once at startup and never
// released, because they are logos. Here a new one arrives every time the
// preview is refreshed, so this owns its texture and frees the old one --
// otherwise every preview would leak a few megabytes of VRAM.

namespace image {

// Holds one texture and releases it on destruction or replacement. Not
// copyable, because two of these freeing the same texture would be a
// double-free.
class Texture {
public:
    Texture() = default;
    ~Texture();

    Texture(const Texture&) = delete;
    Texture& operator=(const Texture&) = delete;

    // Decodes a PNG and replaces whatever was held. Returns false and keeps
    // the previous texture if decoding or upload fails, so a bad response does
    // not blank a preview that was fine.
    bool load(SDL_GPUDevice* device, const void* data, size_t length);

    // Frees the texture. Safe to call twice, and called by the destructor --
    // but the caller must do it explicitly before the GPU device is destroyed,
    // since releasing a texture needs the device that made it.
    void reset();

    bool valid() const { return texture_ != nullptr; }
    int width() const { return width_; }
    int height() const { return height_; }

    // The ImGui SDLGPU3 backend expects ImTextureID to be the SDL_GPUTexture*
    // itself, so this can be handed straight to ImGui::Image.
    ImTextureID id() const { return (ImTextureID)(intptr_t)texture_; }

private:
    SDL_GPUDevice* device_ = nullptr;
    SDL_GPUTexture* texture_ = nullptr;
    int width_ = 0;
    int height_ = 0;
};

}  // namespace image
