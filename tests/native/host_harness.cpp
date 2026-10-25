// Minimal host harness: loads the AI Auto Sequencer plugin exactly the way
// xLights' ServiceManager does (dlopen + xlCreateAIService / xlDestroyAIService)
// and exercises the C ABI end to end. Used by tests/test_native_plugin.py and
// handy for manual verification:
//
//   ./build/host_harness ./build/AIAutoSequencer.so [agentUrl]
//
// When agentUrl is given, the service is pointed at that agent so the
// forwarding path (rather than the built-in fallback) is exercised.

#include <dlfcn.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <list>
#include <string>
#include <vector>

#include "aiBase.h"
#include "aiPlugin.h"

#define CHECK(cond, msg)                                                     \
    do {                                                                     \
        if (!(cond)) {                                                       \
            std::fprintf(stderr, "FAIL: %s\n", msg);                         \
            return 1;                                                        \
        }                                                                    \
    } while (0)

int main(int argc, char** argv) {
    const char* libPath = argc > 1 ? argv[1] : "build/AIAutoSequencer.so";

    void* handle = dlopen(libPath, RTLD_NOW | RTLD_LOCAL);
    if (handle == nullptr) {
        std::fprintf(stderr, "FAIL: dlopen: %s\n", dlerror());
        return 1;
    }

    auto create = reinterpret_cast<xlCreateAIServiceFn>(
        dlsym(handle, "xlCreateAIService"));
    auto destroy = reinterpret_cast<xlDestroyAIServiceFn>(
        dlsym(handle, "xlDestroyAIService"));
    CHECK(create != nullptr, "missing export xlCreateAIService");
    CHECK(destroy != nullptr, "missing export xlDestroyAIService");

    // xLights passes its ServiceManager; nullptr exercises the plugin's
    // no-host-linkage guarantee (a well-behaved plugin never dereferences it).
    aiBase* service = create(nullptr);
    CHECK(service != nullptr, "xlCreateAIService returned null");

    const char* agentUrl = argc > 2 ? argv[2] : nullptr;
    if (agentUrl != nullptr) {
        service->SetProperty("AIAutoSequencer.AgentUrl", std::string(agentUrl));
    }

    int rc = 0;
    do {
        CHECK(service->GetLLMName() == "AI Auto Sequencer", "unexpected service name");

        const auto types = service->GetTypes();
        bool hasPrompt = false, hasPalette = false;
        for (auto t : types) {
            if (t == aiType::PROMPT) hasPrompt = true;
            if (t == aiType::COLORPALETTES) hasPalette = true;
        }
        CHECK(hasPrompt && hasPalette, "service must advertise PROMPT + COLORPALETTES");

        CHECK(service->IsAvailable(), "service must be available (agent or fallback)");
        CHECK(service->IsEnabled(), "service should enable its capabilities on load");

        // Properties must include the agent URL row.
        const auto props = service->GetProperties();
        bool sawUrl = false;
        for (const auto& p : props) {
            if (p.id == "AIAutoSequencer.AgentUrl") sawUrl = true;
        }
        CHECK(sawUrl, "GetProperties must expose AIAutoSequencer.AgentUrl");

        // Prompt path: agent may or may not be running -- both are OK, the
        // answer must exist either way and never crash.
        const auto reply = service->CallLLM("christmas classic, red and green");
        CHECK(reply.second, "CallLLM must succeed (agent or built-in fallback)");
        CHECK(!reply.first.empty(), "CallLLM must return text");
        CHECK(reply.first.find("Style:") != std::string::npos,
              "CallLLM answer should contain a style suggestion");
        std::printf("CallLLM -> %s\n",
                    reply.first.substr(0, 72).c_str());
        // The agent's answer carries a "Why:" line; the built-in fallback
        // never writes one. Report the source so tests can assert forwarding.
        const bool fromAgent = reply.first.find("Why:") != std::string::npos;
        std::printf("answer-source: %s\n", fromAgent ? "agent" : "builtin");
        if (agentUrl != nullptr) {
            CHECK(fromAgent, "expected the agent's answer, not the built-in fallback");
        }

        // Palette path.
        const auto palette = service->GenerateColorPalette("winter wonderland ice");
        CHECK(palette.error.empty(), "GenerateColorPalette should not error");
        CHECK(palette.colors.size() >= 3, "palette must carry colours");
        CHECK(palette.colors[0].hexValue.rfind("#", 0) == 0,
              "palette colours must be hex strings");
        std::printf("GenerateColorPalette -> %zu colours (%s)\n",
                    palette.colors.size(),
                    palette.colors[0].hexValue.c_str());

        // Settings round trip must not crash (file is written to the user's
        // config dir by SaveSettings).
        service->SaveSettings();
        service->LoadSettings();

        // Property setters.
        service->SetProperty("AIAutoSequencer.TimeoutMs", 4321);
        service->SetProperty("AIAutoSequencer.EnablePrompt", false);
        CHECK(!service->IsEnabledForType(aiType::PROMPT),
              "EnablePrompt=false must disable the PROMPT capability");
        service->SetProperty("AIAutoSequencer.EnablePrompt", true);
        CHECK(service->IsEnabledForType(aiType::PROMPT),
              "EnablePrompt=true must re-enable the PROMPT capability");
        service->SetProperty("AIAutoSequencer.AgentUrl",
                             std::string("http://127.0.0.1:8756/"));
        service->SetProperty("AIAutoSequencer.TimeoutMs", 4321);

        std::printf("harness OK\n");
    } while (false);

    destroy(service);
    dlclose(handle);
    return rc;
}
