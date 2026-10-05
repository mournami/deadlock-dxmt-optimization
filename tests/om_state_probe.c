#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <stdio.h>
#include <string.h>

/* Real offscreen draws exercise repeated states, changed factors/masks/refs,
 * new encoders after readback, ClearState and deferred command-list restore.
 * This checks correctness; it is not a Deadlock FPS benchmark. */
typedef HRESULT (WINAPI *compile_fn)(LPCVOID, SIZE_T, LPCSTR, const D3D_SHADER_MACRO *,
    ID3DInclude *, LPCSTR, LPCSTR, UINT, UINT, ID3DBlob **, ID3DBlob **);

static HRESULT shader(compile_fn compile, const char *text, const char *profile, ID3DBlob **blob) {
    ID3DBlob *error = NULL;
    HRESULT hr = compile(text, strlen(text), "state-test", NULL, NULL, "main", profile, 0, 0, blob, &error);
    if (FAILED(hr) && error) printf("shader_error: %s\n", (const char *)ID3D10Blob_GetBufferPointer(error));
    if (error) ID3D10Blob_Release(error);
    return hr;
}

static int read_pixel(ID3D11DeviceContext *ctx, ID3D11Texture2D *source, ID3D11Texture2D *staging,
                      int r, int g, int b) {
    ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource *)staging, (ID3D11Resource *)source);
    D3D11_MAPPED_SUBRESOURCE map;
    HRESULT hr = ID3D11DeviceContext_Map(ctx, (ID3D11Resource *)staging, 0, D3D11_MAP_READ, 0, &map);
    if (FAILED(hr)) return 0;
    unsigned char *p = map.pData;
    int ok = p[0] >= r-1 && p[0] <= r+1 && p[1] >= g-1 && p[1] <= g+1 && p[2] >= b-1 && p[2] <= b+1;
    if (!ok) printf("pixel_mismatch actual=%u,%u,%u expected=%d,%d,%d\n",p[0],p[1],p[2],r,g,b);
    ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource *)staging, 0);
    return ok;
}

static void bind_draw_state(ID3D11DeviceContext *ctx, ID3D11RenderTargetView *rtv, ID3D11DepthStencilView *dsv,
                 ID3D11VertexShader *vs, ID3D11PixelShader *ps, ID3D11RasterizerState *rs) {
    D3D11_VIEWPORT vp = {0,0,4,4,0,1};
    ID3D11DeviceContext_OMSetRenderTargets(ctx, 1, &rtv, dsv);
    ID3D11DeviceContext_IASetPrimitiveTopology(ctx, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_VSSetShader(ctx, vs, NULL, 0);
    ID3D11DeviceContext_PSSetShader(ctx, ps, NULL, 0);
    ID3D11DeviceContext_RSSetState(ctx, rs);
    ID3D11DeviceContext_RSSetViewports(ctx, 1, &vp);
}

int main(void) {
    int result = 1;
    HRESULT hr = E_FAIL;
    HMODULE d3d = LoadLibraryA("d3d11.dll"), compiler = LoadLibraryA("d3dcompiler_47.dll");
    if (!d3d || !compiler) { puts("required DLL missing"); return 1; }
    PFN_D3D11_CREATE_DEVICE create = (PFN_D3D11_CREATE_DEVICE)GetProcAddress(d3d,"D3D11CreateDevice");
    compile_fn compile = (compile_fn)GetProcAddress(compiler,"D3DCompile");
    if (!create || !compile) return 1;
    ID3D11Device *dev = NULL;
    ID3D11DeviceContext *ctx = NULL, *deferred = NULL;
    ID3D11CommandList *list = NULL;
    ID3D11Texture2D *color = NULL, *staging = NULL, *depth = NULL;
    ID3D11RenderTargetView *rtv = NULL;
    ID3D11DepthStencilView *dsv = NULL;
    ID3D11BlendState *blend = NULL;
    ID3D11DepthStencilState *ds = NULL;
    ID3D11RasterizerState *rs = NULL;
    ID3D11VertexShader *vs = NULL;
    ID3D11PixelShader *ps = NULL;
    ID3DBlob *vcode = NULL, *pcode = NULL;
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
#define CHECK(call) do { hr = (call); if (FAILED(hr)) { printf("failed %s: %08lx\n",#call,(unsigned long)hr); goto done; } } while(0)
#define PIXEL(r,g,b) do { if (!read_pixel(ctx,color,staging,r,g,b)) goto done; } while(0)
    CHECK(create(NULL,D3D_DRIVER_TYPE_HARDWARE,NULL,0,&fl,1,D3D11_SDK_VERSION,&dev,NULL,&ctx));
    puts("device_created");
    CHECK(shader(compile,"float4 main(uint id:SV_VertexID):SV_Position { float2 p[3]={float2(-1,-1),float2(-1,3),float2(3,-1)}; return float4(p[id],0.5,1); }","vs_5_0",&vcode));
    CHECK(shader(compile,"float4 main():SV_Target { return float4(1,1,1,1); }","ps_5_0",&pcode));
    CHECK(ID3D11Device_CreateVertexShader(dev,ID3D10Blob_GetBufferPointer(vcode),ID3D10Blob_GetBufferSize(vcode),NULL,&vs));
    CHECK(ID3D11Device_CreatePixelShader(dev,ID3D10Blob_GetBufferPointer(pcode),ID3D10Blob_GetBufferSize(pcode),NULL,&ps));
    D3D11_TEXTURE2D_DESC td = {0};
    td.Width=td.Height=4; td.MipLevels=td.ArraySize=td.SampleDesc.Count=1;
    td.Format=DXGI_FORMAT_R8G8B8A8_UNORM; td.BindFlags=D3D11_BIND_RENDER_TARGET;
    CHECK(ID3D11Device_CreateTexture2D(dev,&td,NULL,&color));
    CHECK(ID3D11Device_CreateRenderTargetView(dev,(ID3D11Resource *)color,NULL,&rtv));
    td.Usage=D3D11_USAGE_STAGING; td.BindFlags=0; td.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    CHECK(ID3D11Device_CreateTexture2D(dev,&td,NULL,&staging));
    td.Usage=D3D11_USAGE_DEFAULT; td.BindFlags=D3D11_BIND_DEPTH_STENCIL; td.CPUAccessFlags=0;
    td.Format=DXGI_FORMAT_D24_UNORM_S8_UINT;
    CHECK(ID3D11Device_CreateTexture2D(dev,&td,NULL,&depth));
    CHECK(ID3D11Device_CreateDepthStencilView(dev,(ID3D11Resource *)depth,NULL,&dsv));
    D3D11_BLEND_DESC bd = {0};
    bd.RenderTarget[0].BlendEnable=TRUE; bd.RenderTarget[0].SrcBlend=D3D11_BLEND_BLEND_FACTOR;
    bd.RenderTarget[0].DestBlend=D3D11_BLEND_ZERO; bd.RenderTarget[0].BlendOp=D3D11_BLEND_OP_ADD;
    bd.RenderTarget[0].SrcBlendAlpha=D3D11_BLEND_ONE; bd.RenderTarget[0].DestBlendAlpha=D3D11_BLEND_ZERO;
    bd.RenderTarget[0].BlendOpAlpha=D3D11_BLEND_OP_ADD; bd.RenderTarget[0].RenderTargetWriteMask=15;
    CHECK(ID3D11Device_CreateBlendState(dev,&bd,&blend));
    D3D11_DEPTH_STENCIL_DESC dd = {0};
    dd.DepthFunc=D3D11_COMPARISON_ALWAYS; dd.StencilEnable=TRUE;
    dd.StencilReadMask=dd.StencilWriteMask=255;
    dd.FrontFace.StencilFunc=D3D11_COMPARISON_EQUAL;
    dd.FrontFace.StencilFailOp=dd.FrontFace.StencilDepthFailOp=dd.FrontFace.StencilPassOp=D3D11_STENCIL_OP_KEEP;
    dd.BackFace=dd.FrontFace;
    CHECK(ID3D11Device_CreateDepthStencilState(dev,&dd,&ds));
    D3D11_RASTERIZER_DESC rd = {0}; rd.FillMode=D3D11_FILL_SOLID; rd.CullMode=D3D11_CULL_NONE; rd.DepthClipEnable=TRUE;
    CHECK(ID3D11Device_CreateRasterizerState(dev,&rd,&rs));
    bind_draw_state(ctx,rtv,dsv,vs,ps,rs);
    ID3D11DeviceContext_ClearDepthStencilView(ctx,dsv,D3D11_CLEAR_DEPTH|D3D11_CLEAR_STENCIL,1,5);
    float black[4]={0,0,0,1}, factor[4]={.25f,.5f,.75f,1};
    ID3D11DeviceContext_ClearRenderTargetView(ctx,rtv,black);
    for (int i=0;i<128;++i) {
        ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,~0u);
        ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,5);
        ID3D11DeviceContext_Draw(ctx,3,0);
    }
    PIXEL(64,128,191);
    /* Readback ended the pass: repeated state must still bind in a new one. */
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,~0u);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,5);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(64,128,191);
    factor[0]=.75f; factor[1]=.25f; factor[2]=.5f;
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,~0u);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,6);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(64,128,191);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,5);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(191,64,128);
    ID3D11DeviceContext_ClearRenderTargetView(ctx,rtv,black);
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,0);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(0,0,0);
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,NULL,~0u);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(255,255,255);
    /* Bitwise comparison must preserve -0 and NaN payloads exposed by Get. */
    unsigned int bits[4]={0x80000000u,0x7fc12345u,0x3f800000u,0x3f000000u};
    float special[4], observed[4]; unsigned int observed_mask=0;
    memcpy(special,bits,sizeof(bits));
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,special,123);
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,special,123);
    ID3D11DeviceContext_OMGetBlendState(ctx,NULL,observed,&observed_mask);
    if (memcmp(special,observed,sizeof(special)) || observed_mask!=123) { puts("state_bits_mismatch"); goto done; }
    ID3D11DeviceContext_ClearState(ctx); bind_draw_state(ctx,rtv,dsv,vs,ps,rs);
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,~0u);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,5);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(191,64,128);
    ID3D11DeviceContext_ClearDepthStencilView(ctx,dsv,D3D11_CLEAR_DEPTH,1,0);
    ID3D11DeviceContext_OMSetBlendState(ctx,NULL,NULL,~0u);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,NULL,0);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(255,255,255);
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,~0u);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,5);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(191,64,128);
    CHECK(ID3D11Device_CreateDeferredContext(dev,0,&deferred));
    bind_draw_state(deferred,rtv,dsv,vs,ps,rs);
    for (int i=0;i<16;++i) {
        ID3D11DeviceContext_OMSetBlendState(deferred,blend,NULL,~0u);
        ID3D11DeviceContext_OMSetDepthStencilState(deferred,ds,5);
        ID3D11DeviceContext_Draw(deferred,3,0);
    }
    CHECK(ID3D11DeviceContext_FinishCommandList(deferred,TRUE,&list));
    ID3D11DeviceContext_ExecuteCommandList(ctx,list,TRUE); PIXEL(255,255,255);
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,~0u);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,5);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(191,64,128);
    ID3D11DeviceContext_ExecuteCommandList(ctx,list,FALSE); PIXEL(255,255,255);
    bind_draw_state(ctx,rtv,dsv,vs,ps,rs);
    ID3D11DeviceContext_OMSetBlendState(ctx,blend,factor,~0u);
    ID3D11DeviceContext_OMSetDepthStencilState(ctx,ds,5);
    ID3D11DeviceContext_Draw(ctx,3,0); PIXEL(191,64,128);
    puts("om_state_readback_passed: repeats, factor, mask, stencil, encoder reset, ClearState, deferred restore");
    result=0;
done:
    if (ctx) { ID3D11DeviceContext_ClearState(ctx); ID3D11DeviceContext_Flush(ctx); }
#define RELEASE(obj,type) if(obj) type##_Release(obj)
    RELEASE(list,ID3D11CommandList); RELEASE(deferred,ID3D11DeviceContext); RELEASE(rs,ID3D11RasterizerState);
    RELEASE(ds,ID3D11DepthStencilState); RELEASE(blend,ID3D11BlendState);
    RELEASE(vs,ID3D11VertexShader); RELEASE(ps,ID3D11PixelShader);
    RELEASE(vcode,ID3D10Blob); RELEASE(pcode,ID3D10Blob);
    RELEASE(dsv,ID3D11DepthStencilView); RELEASE(rtv,ID3D11RenderTargetView);
    RELEASE(depth,ID3D11Texture2D); RELEASE(staging,ID3D11Texture2D); RELEASE(color,ID3D11Texture2D);
    RELEASE(ctx,ID3D11DeviceContext); RELEASE(dev,ID3D11Device);
    FreeLibrary(compiler); FreeLibrary(d3d);
    return result;
}
