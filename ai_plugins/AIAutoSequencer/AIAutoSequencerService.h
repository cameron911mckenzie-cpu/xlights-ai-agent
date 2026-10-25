#pragma once

/***************************************************************
 * xLights AI Auto Sequencer - addon plugin service
 *
 * A third-party AI service for xLights (GPLv3, same as xLights itself).
 * Drop the built library into the `ai_plugins/` directory next to the
 * xLights executable and it appears under Preferences -> Services.
 *
 * The service forwards Prompt / Color Palette requests to the local
 * xlights-ai-agent daemon (`xlights-ai serve`), which does the real work:
 * beat-driven auto-sequencing, prompt parsing and style suggestions.
 * When the daemon is not running it degrades to a small built-in keyword
 * style engine so xLights always gets a useful answer.
 **************************************************************/

#include "aiBase.h"

#include <string>

class AIAutoSequencerService : public aiBase {
public:
    explicit AIAutoSequencerService(ServiceManager* sm);
    ~AIAutoSequencerService() override = default;

    void SaveSettings() const override;
    void LoadSettings() override;

    [[nodiscard]] std::string GetLLMName() const override;
    [[nodiscard]] bool IsAvailable() const override;
    [[nodiscard]] std::list<aiType::TYPE> GetTypes() const override;

    [[nodiscard]] std::pair<std::string, bool> CallLLM(const std::string& prompt) const override;

    [[nodiscard]] std::vector<ServiceProperty> GetProperties() const override;
    void SetProperty(const std::string& id, bool value) override;
    void SetProperty(const std::string& id, int value) override;
    void SetProperty(const std::string& id, const std::string& value) override;

    [[nodiscard]] AIColorPalette GenerateColorPalette(const std::string& prompt) const override;

private:
    bool _enablePrompt = true;
    bool _enablePalette = true;
    bool _builtinFallback = true;
    std::string _agentUrl = "http://127.0.0.1:8756";
    int _timeoutMs = 8000;

    // POST a JSON body to the local agent; returns true and the response body
    // on HTTP 200, false otherwise (network error, non-200, bad URL...).
    [[nodiscard]] bool AgentPost(const std::string& path,
                                 const std::string& jsonBody,
                                 std::string& responseBody) const;
    [[nodiscard]] bool AgentPing() const;
    [[nodiscard]] std::string ConfigFilePath() const;

    [[nodiscard]] static std::string BuiltinSuggestionText(const std::string& prompt);
    [[nodiscard]] static AIColorPalette BuiltinPalette(const std::string& prompt);
};
