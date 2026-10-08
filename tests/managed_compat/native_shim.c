// Deterministic ABI peer for managed lifetime/compatibility tests, never a product library.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#define strdup _strdup
#else
#define API __attribute__((visibility("default")))
#endif

typedef void (*StringCb)(void*, const char*);
typedef void (*NewCb)(void*, int64_t, const char*, int, int);
typedef void (*ProxyCb)(void*, const char*, int);
typedef struct { StringCb cdp, legacy; void* cdp_user; NewCb popup; void* popup_user; } View;
static int mode, sends, freed, legacy_offset, proxy_mode, legacy_selected;
static int64_t pending;
static ProxyCb late_proxy;
static void* late_proxy_user;
API void probe_mode(int value) { mode=value; sends=0; }
API int probe_sends(void) { return sends; }
API int probe_freed(void) { return freed; }
API void probe_legacy_offset(int value) { legacy_offset=value; }
API void probe_proxy_mode(int value) { proxy_mode=value; }
API void probe_late_proxy(void) { if(late_proxy) late_proxy(late_proxy_user,(const char*)1,0); }
API int probe_legacy_selected(void) { return legacy_selected; }
API int arupa_kernel_abi_minor(void) { return 32; }
API int arupa_kernel_supports(const char* name) { return !strcmp(name,"window.pending.discard.v1"); }
API int arupa_webview_create_checked(void* kernel,void* cfg,size_t cfg_size,void* callbacks,size_t cb_size,void* sink,size_t sink_size,void** result) {
  View* v=(View*)calloc(1,sizeof(View));
  memcpy(&v->legacy,(char*)callbacks+legacy_offset,sizeof(void*));
  *result=v; return 0;
}
API void arupa_webview_destroy(View* v) { free(v); }
API void arupa_webview_cdp_attach(View* v,StringCb cb,void* user) { v->cdp=cb; v->cdp_user=user; }
API void arupa_webview_cdp_send(View* v,const char* command) {
  int id=0; sscanf(command,"{\"id\":%d",&id); ++sends;
  if(mode==4) return;
  const char* body=(mode==2 || (mode==1 && sends==1))
    ? "\"error\":{\"code\":-32001,\"message\":\"ARUPA_CDP_SESSION_REBOUND\"}"
    : mode==3 ? "\"error\":{\"code\":-1,\"message\":\"other error\"}" : "\"result\":{\"ok\":true}";
  char result[512]; snprintf(result,sizeof(result),"{\"id\":%d,%s}",id,body); v->cdp(v->cdp_user,result);
}
API void arupa_webview_set_new_contents_callback(View* v,NewCb cb,void* user) { v->popup=cb; v->popup_user=user; }
API void probe_open(View* v) {
  if(v->popup) { pending=123; v->popup(v->popup_user,pending,"https://example.test",3,1); }
  else if(v->legacy) v->legacy(NULL,"https://example.test");
}
API int arupa_webview_discard_pending(int64_t token) { if(token!=pending || !pending)return 4; pending=0;return 0; }
API int arupa_webview_adopt_pending(void* k,int64_t token,void* cfg,void* cb,void* sink,void** view) {
  if(arupa_webview_discard_pending(token)) return 4;
  return arupa_webview_create_checked(k,cfg,0,cb,0,sink,0,view);
}
API void arupa_webview_lookup_proxy_for_url(View* v,const char* url,ProxyCb cb,void* user) {
  if(proxy_mode==2){late_proxy=cb;late_proxy_user=user;return;}
  cb(user,proxy_mode==1?NULL:"PROXY real.test:8080",proxy_mode==1?-111:0);
}
API void* arupa_webview_list_frames(View* v) { return strdup("[{\"frame_id\":7,\"is_main\":true}]"); }
API void arupa_free(void* p) { ++freed;free(p); }
API void* arupa_kernel_get_extension_storage_state(const char* id) { return strdup("{\"state\":\"needs_merge\"}"); }
API void* arupa_kernel_get_extension_partition_key(const char* id) { return strdup("/actual/partition"); }
API void* arupa_kernel_clear_extension_storage(const char* id) { return strdup("{\"code\":0}"); }
API int arupa_kernel_forget_extension_storage_migration(const char* id) { return 1; }
API int arupa_webview_use_legacy_extension_partition(View* v,const char* id) { ++legacy_selected;return strcmp(id,"missing")!=0; }
