#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <stdio.h>

/* A headless integration check: create the real DXMT device, clear a Metal
 * texture, copy it to staging and read pixels back. Does not start Steam/game. */
int main(void) {
    HMODULE dll = LoadLibraryA("d3d11.dll");
    if (!dll) { printf("LoadLibrary failed: %lu\n", GetLastError()); return 1; }
    PFN_D3D11_CREATE_DEVICE create = (PFN_D3D11_CREATE_DEVICE)GetProcAddress(dll, "D3D11CreateDevice");
    if (!create) { puts("D3D11CreateDevice is missing"); return 2; }
    ID3D11Device *device = NULL;
    ID3D11DeviceContext *context = NULL;
    ID3D11Texture2D *texture = NULL, *staging = NULL;
    ID3D11RenderTargetView *view = NULL;
    D3D_FEATURE_LEVEL request = D3D_FEATURE_LEVEL_11_0, actual;
    HRESULT hr = create(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, &request, 1,
                        D3D11_SDK_VERSION, &device, &actual, &context);
    int result = 3;
    const char *step = "CreateDevice";
    if (FAILED(hr)) goto done;
    printf("device_created feature_level=0x%x\n", actual);
    D3D11_TEXTURE2D_DESC desc = {0};
    desc.Width = desc.Height = 4;
    desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_RENDER_TARGET;
    step = "CreateTexture2D";
    hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &texture);
    if (FAILED(hr)) goto done;
    step = "CreateRenderTargetView";
    hr = ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)texture, NULL, &view);
    if (FAILED(hr)) goto done;
    const float color[4] = {0.25f, 0.5f, 0.75f, 1.f};
    ID3D11DeviceContext_ClearRenderTargetView(context, view, color);
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    step = "CreateStagingTexture";
    hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &staging);
    if (FAILED(hr)) goto done;
    ID3D11DeviceContext_CopyResource(context, (ID3D11Resource *)staging, (ID3D11Resource *)texture);
    D3D11_MAPPED_SUBRESOURCE mapped;
    step = "MapReadback";
    hr = ID3D11DeviceContext_Map(context, (ID3D11Resource *)staging, 0, D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hr)) goto done;
    const unsigned char *pixel = mapped.pData;
    printf("gpu_readback RGBA=%u,%u,%u,%u\n", pixel[0], pixel[1], pixel[2], pixel[3]);
    result = (pixel[0] >= 63 && pixel[0] <= 65 && pixel[1] >= 127 && pixel[1] <= 129 &&
              pixel[2] >= 190 && pixel[2] <= 192 && pixel[3] == 255) ? 0 : 4;
    ID3D11DeviceContext_Unmap(context, (ID3D11Resource *)staging, 0);
done:
    if (FAILED(hr)) printf("%s failed: 0x%08lx\n", step, (unsigned long)hr);
    if (context) { ID3D11DeviceContext_ClearState(context); ID3D11DeviceContext_Flush(context); }
    if (view) ID3D11RenderTargetView_Release(view);
    if (staging) ID3D11Texture2D_Release(staging);
    if (texture) ID3D11Texture2D_Release(texture);
    if (context) ID3D11DeviceContext_Release(context);
    if (device) ID3D11Device_Release(device);
    FreeLibrary(dll);
    return result;
}
