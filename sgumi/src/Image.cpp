#include "Image.hpp"

// stb_image is a single-header library: exactly one translation unit in the
// program may define this before including it. Keeping that definition in a
// .cpp rather than the header is what stops a second includer emitting a
// duplicate copy of the implementation.
#define STB_IMAGE_IMPLEMENTATION
#include <stb_image.h>

#include <cstring>
#include <iostream>

namespace image {

Texture::~Texture() {
    reset();
}

void Texture::reset() {
    if (!texture_) {
        return;
    }

    // Documented as freeing the texture once the GPU is finished with it, so
    // this is safe even for a texture the frame in flight is still sampling.
    SDL_ReleaseGPUTexture(device_, texture_);

    texture_ = nullptr;
    width_ = 0;
    height_ = 0;
}

bool Texture::load(SDL_GPUDevice* device, const void* data, size_t length) {
    int width = 0;
    int height = 0;
    int channels = 0;

    // Forced to 4 channels: lipgloss sends RGB, the GPU format below is RGBA,
    // and letting stb convert is cheaper than a second format to handle.
    unsigned char* pixels = stbi_load_from_memory(
        static_cast<const unsigned char*>(data),
        static_cast<int>(length),
        &width,
        &height,
        &channels,
        4);

    if (!pixels) {
        std::cerr << "Preview: could not decode PNG: " << stbi_failure_reason()
                  << std::endl;
        return false;
    }

    SDL_GPUTextureCreateInfo texInfo = {};
    texInfo.type = SDL_GPU_TEXTURETYPE_2D;
    texInfo.format = SDL_GPU_TEXTUREFORMAT_R8G8B8A8_UNORM;
    texInfo.usage = SDL_GPU_TEXTUREUSAGE_SAMPLER;
    texInfo.width = static_cast<Uint32>(width);
    texInfo.height = static_cast<Uint32>(height);
    texInfo.layer_count_or_depth = 1;
    texInfo.num_levels = 1;

    SDL_GPUTexture* texture = SDL_CreateGPUTexture(device, &texInfo);

    if (!texture) {
        std::cerr << "Preview: could not create texture: " << SDL_GetError()
                  << std::endl;
        stbi_image_free(pixels);
        return false;
    }

    const size_t byteCount = static_cast<size_t>(width) * height * 4;

    SDL_GPUTransferBufferCreateInfo tbInfo = {};
    tbInfo.usage = SDL_GPU_TRANSFERBUFFERUSAGE_UPLOAD;
    tbInfo.size = static_cast<Uint32>(byteCount);

    SDL_GPUTransferBuffer* transfer = SDL_CreateGPUTransferBuffer(device, &tbInfo);

    if (!transfer) {
        std::cerr << "Preview: could not create transfer buffer: "
                  << SDL_GetError() << std::endl;
        SDL_ReleaseGPUTexture(device, texture);
        stbi_image_free(pixels);
        return false;
    }

    void* mapped = SDL_MapGPUTransferBuffer(device, transfer, false);

    if (!mapped) {
        std::cerr << "Preview: could not map transfer buffer: "
                  << SDL_GetError() << std::endl;
        SDL_ReleaseGPUTransferBuffer(device, transfer);
        SDL_ReleaseGPUTexture(device, texture);
        stbi_image_free(pixels);
        return false;
    }

    std::memcpy(mapped, pixels, byteCount);
    SDL_UnmapGPUTransferBuffer(device, transfer);
    stbi_image_free(pixels);

    SDL_GPUCommandBuffer* cmd = SDL_AcquireGPUCommandBuffer(device);
    SDL_GPUCopyPass* copyPass = SDL_BeginGPUCopyPass(cmd);

    SDL_GPUTextureTransferInfo src = {};
    src.transfer_buffer = transfer;

    SDL_GPUTextureRegion dst = {};
    dst.texture = texture;
    dst.w = static_cast<Uint32>(width);
    dst.h = static_cast<Uint32>(height);
    dst.d = 1;

    SDL_UploadToGPUTexture(copyPass, &src, &dst, false);
    SDL_EndGPUCopyPass(copyPass);
    SDL_SubmitGPUCommandBuffer(cmd);
    SDL_ReleaseGPUTransferBuffer(device, transfer);

    // Only now that the new one is definitely good: a failed decode above
    // leaves the previous preview on screen rather than blanking it.
    reset();

    device_ = device;
    texture_ = texture;
    width_ = width;
    height_ = height;

    return true;
}

}  // namespace image
