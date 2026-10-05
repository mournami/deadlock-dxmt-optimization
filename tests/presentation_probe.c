#define COBJMACROS
#include <windows.h>
#include <initguid.h>
#include <d3d11.h>
#include <dxgi1_3.h>
#include <stdio.h>

/* A small, non-activating private test window exercises real Present,
 * waitable-swapchain pacing and resize. Never starts Steam or a game. */
int main(void) {
    int result = 1;
    HRESULT hr = E_FAIL;
    HMODULE dll = LoadLibraryA("d3d11.dll");
    if (!dll) return 1;
    PFN_D3D11_CREATE_DEVICE create = (PFN_D3D11_CREATE_DEVICE)GetProcAddress(dll, "D3D11CreateDevice");
    if (!create) { FreeLibrary(dll); return 1; }
    ID3D11Device *dev = NULL;
    ID3D11DeviceContext *ctx = NULL;
    IDXGIDevice *dxgi = NULL;
    IDXGIAdapter *adapter = NULL;
    IDXGIFactory2 *factory = NULL;
    IDXGISwapChain1 *swap = NULL;
    IDXGISwapChain2 *swap2 = NULL;
    ID3D11Texture2D *buffer = NULL;
    ID3D11RenderTargetView *view = NULL;
    HANDLE waitable = NULL;
    HWND window = NULL;
    WNDCLASSA klass = {0};
    klass.lpfnWndProc = DefWindowProcA;
    klass.hInstance = GetModuleHandleA(NULL);
    klass.lpszClassName = "DXMTPresentationProbe";
    if (!RegisterClassA(&klass)) goto done;
    window = CreateWindowExA(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, klass.lpszClassName,
        "DXMT presentation probe", WS_OVERLAPPEDWINDOW, 0, 0, 160, 120,
        NULL, NULL, klass.hInstance, NULL);
    if (!window) goto done;
    ShowWindow(window, SW_SHOWNOACTIVATE);
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
#define CHECK(call) do { hr = (call); if (FAILED(hr)) { printf("failed %s: %08lx\n",#call,(unsigned long)hr); goto done; } } while(0)
    CHECK(create(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, &level, 1, D3D11_SDK_VERSION, &dev, NULL, &ctx));
    puts("device_created");
    CHECK(ID3D11Device_QueryInterface(dev, &IID_IDXGIDevice, (void **)&dxgi));
    CHECK(IDXGIDevice_GetAdapter(dxgi, &adapter));
    CHECK(IDXGIAdapter_GetParent(adapter, &IID_IDXGIFactory2, (void **)&factory));
    DXGI_SWAP_CHAIN_DESC1 desc = {0};
    desc.Width = desc.Height = 64;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 2;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    desc.Flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
    CHECK(IDXGIFactory2_CreateSwapChainForHwnd(factory, (IUnknown *)dev, window, &desc, NULL, NULL, &swap));
    CHECK(IDXGISwapChain1_QueryInterface(swap, &IID_IDXGISwapChain2, (void **)&swap2));
    CHECK(IDXGISwapChain2_SetMaximumFrameLatency(swap2, 1));
    waitable = IDXGISwapChain2_GetFrameLatencyWaitableObject(swap2);
    if (!waitable) goto done;
    CHECK(IDXGISwapChain1_Present(swap, 0, DXGI_PRESENT_TEST));
    for (unsigned frame = 0; frame < 16; ++frame) {
        MSG msg;
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        if (frame == 8)
            CHECK(IDXGISwapChain1_ResizeBuffers(swap, 0, 96, 64, DXGI_FORMAT_UNKNOWN, desc.Flags));
        if (WaitForSingleObject(waitable, 5000) != WAIT_OBJECT_0) {
            puts("frame latency object timed out");
            goto done;
        }
        CHECK(IDXGISwapChain1_GetBuffer(swap, 0, &IID_ID3D11Texture2D, (void **)&buffer));
        CHECK(ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource *)buffer, NULL, &view));
        const float color[4] = {0.2f, 0.3f, 0.4f, 1};
        ID3D11DeviceContext_ClearRenderTargetView(ctx, view, color);
        CHECK(IDXGISwapChain1_Present(swap, 0, 0));
        ID3D11RenderTargetView_Release(view); view = NULL;
        ID3D11Texture2D_Release(buffer); buffer = NULL;
    }
    result = 0;
    puts("presentation_probe_passed");
done:
    if (view) ID3D11RenderTargetView_Release(view);
    if (buffer) ID3D11Texture2D_Release(buffer);
    if (ctx) { ID3D11DeviceContext_ClearState(ctx); ID3D11DeviceContext_Flush(ctx); }
    if (waitable) CloseHandle(waitable);
    if (swap2) IDXGISwapChain2_Release(swap2);
    if (swap) IDXGISwapChain1_Release(swap);
    if (factory) IDXGIFactory2_Release(factory);
    if (adapter) IDXGIAdapter_Release(adapter);
    if (dxgi) IDXGIDevice_Release(dxgi);
    if (ctx) ID3D11DeviceContext_Release(ctx);
    if (dev) ID3D11Device_Release(dev);
    if (window) DestroyWindow(window);
    UnregisterClassA(klass.lpszClassName, klass.hInstance);
    FreeLibrary(dll);
    return result;
}
