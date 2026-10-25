/***************************************************************
 * xLights AI Auto Sequencer - addon plugin service implementation.
 * See AIAutoSequencerService.h for the overview.
 *
 * No dependencies beyond the C++ standard library and OS sockets:
 * the xLights AI plugin ABI is designed so plugin DLLs need no
 * linkage back into the xLights binary.
 **************************************************************/

#include "AIAutoSequencerService.h"
#include "aiPlugin.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <vector>

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  pragma comment(lib, "ws2_32.lib")
using socket_t = SOCKET;
static const socket_t kInvalidSocket = INVALID_SOCKET;
#else
#  include <netdb.h>
#  include <sys/socket.h>
#  include <sys/time.h>
#  include <unistd.h>
using socket_t = int;
static const socket_t kInvalidSocket = -1;
#endif

namespace {

// ---------------------------------------------------------------------------
// Minimal HTTP/1.1 client (plain http; the agent is a loopback service)
// ---------------------------------------------------------------------------

struct UrlParts {
    std::string host;
    int port = 80;
    std::string path = "/";
};

bool ParseHttpUrl(const std::string& url, UrlParts& out) {
    const std::string scheme = "http://";
    if (url.rfind(scheme, 0) != 0) return false;   // https not supported (local agent)
    std::string rest = url.substr(scheme.size());
    std::string authority;
    const auto slash = rest.find('/');
    if (slash == std::string::npos) {
        authority = rest;
        rest = "/";
    } else {
        authority = rest.substr(0, slash);
        rest = rest.substr(slash);
    }
    if (authority.empty()) return false;
    const auto colon = authority.rfind(':');
    if (colon != std::string::npos && authority.find(']') < colon) {
        // naive handling; IPv6 literals with ports are out of scope
    }
    if (colon != std::string::npos && authority.find(']') == std::string::npos) {
        out.port = std::atoi(authority.substr(colon + 1).c_str());
        out.host = authority.substr(0, colon);
    } else {
        out.host = authority;
        out.port = 80;
    }
    out.path = rest;
    return !out.host.empty() && out.port > 0;
}

std::string JsonEscape(const std::string& in) {
    std::string out;
    out.reserve(in.size() + 8);
    for (unsigned char c : in) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

bool HttpExchange(const UrlParts& url, const std::string& method,
                  const std::string& body, int timeoutMs, std::string& responseBody) {
#ifdef _WIN32
    static bool wsaReady = false;
    if (!wsaReady) {
        WSADATA data;
        if (WSAStartup(MAKEWORD(2, 2), &data) != 0) return false;
        wsaReady = true;
    }
#endif
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    const std::string portStr = std::to_string(url.port);
    if (getaddrinfo(url.host.c_str(), portStr.c_str(), &hints, &res) != 0 || res == nullptr) {
        return false;
    }

    socket_t fd = kInvalidSocket;
    for (addrinfo* ai = res; ai != nullptr; ai = ai->ai_next) {
        fd = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd == kInvalidSocket) continue;
        timeval tv{};
        tv.tv_sec = timeoutMs / 1000;
        tv.tv_usec = (timeoutMs % 1000) * 1000;
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&tv), sizeof(tv));
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&tv), sizeof(tv));
        if (::connect(fd, ai->ai_addr, static_cast<int>(ai->ai_addrlen)) == 0) break;
#ifdef _WIN32
        closesocket(fd);
#else
        ::close(fd);
#endif
        fd = kInvalidSocket;
    }
    freeaddrinfo(res);
    if (fd == kInvalidSocket) return false;

    std::ostringstream req;
    req << method << ' ' << url.path << " HTTP/1.1\r\n"
        << "Host: " << url.host << ':' << url.port << "\r\n"
        << "Accept: application/json\r\n"
        << "Connection: close\r\n";
    if (!body.empty()) {
        req << "Content-Type: application/json\r\n"
            << "Content-Length: " << body.size() << "\r\n";
    }
    req << "\r\n" << body;
    const std::string request = req.str();
    if (::send(fd, request.c_str(), static_cast<int>(request.size()), 0) < 0) {
#ifdef _WIN32
        closesocket(fd);
#else
        ::close(fd);
#endif
        return false;
    }

    std::string raw;
    char buf[4096];
    for (;;) {
        const auto n = ::recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        raw.append(buf, static_cast<size_t>(n));

        // Stop as soon as the framed response is complete.
        const auto he = raw.find("\r\n\r\n");
        if (he != std::string::npos) {
            const std::string headers = raw.substr(0, he);
            size_t want = std::string::npos;
            auto cl = headers.find("Content-Length:");
            if (cl == std::string::npos) cl = headers.find("content-length:");
            if (cl != std::string::npos) {
                want = static_cast<size_t>(
                    std::strtoul(headers.c_str() + cl + 15, nullptr, 10));
            }
            if (want != std::string::npos && raw.size() >= he + 4 + want) break;
        }
        if (raw.size() > 8u * 1024u * 1024u) break;   // sanity cap
    }
#ifdef _WIN32
    closesocket(fd);
#else
    ::close(fd);
#endif

    const auto headerEnd = raw.find("\r\n\r\n");
    if (headerEnd == std::string::npos) return false;
    const std::string headers = raw.substr(0, headerEnd);
    std::string payload = raw.substr(headerEnd + 4);

    const auto statusLineEnd = headers.find("\r\n");
    const std::string statusLine = headers.substr(0, statusLineEnd);
    const auto sp1 = statusLine.find(' ');
    if (sp1 == std::string::npos) return false;
    const int status = std::atoi(statusLine.c_str() + sp1 + 1);
    if (status < 200 || status >= 300) return false;

    // Honour Content-Length when present; otherwise assume Connection: close
    // delivered everything already.
    const auto clPos = headers.find("Content-Length:");
    if (clPos == std::string::npos) {
        const auto clPos2 = headers.find("content-length:");
        if (clPos2 != std::string::npos) {
            const auto len = static_cast<size_t>(std::strtoul(headers.c_str() + clPos2 + 15, nullptr, 10));
            payload.resize(std::min(payload.size(), len));
        }
    } else {
        const auto len = static_cast<size_t>(std::strtoul(headers.c_str() + clPos + 15, nullptr, 10));
        payload.resize(std::min(payload.size(), len));
    }
    responseBody = payload;
    return true;
}

// ---------------------------------------------------------------------------
// Tiny JSON string extraction (our agent controls both ends of the wire)
// ---------------------------------------------------------------------------

bool ExtractJsonString(const std::string& json, const std::string& key, std::string& value) {
    const std::string needle = "\"" + key + "\"";
    auto pos = json.find(needle);
    while (pos != std::string::npos) {
        auto p = json.find(':', pos + needle.size());
        if (p == std::string::npos) return false;
        ++p;
        while (p < json.size() && (json[p] == ' ' || json[p] == '\n' || json[p] == '\t' || json[p] == '\r')) ++p;
        if (p < json.size() && json[p] == '"') {
            ++p;
            std::string out;
            while (p < json.size() && json[p] != '"') {
                if (json[p] == '\\' && p + 1 < json.size()) {
                    ++p;
                    switch (json[p]) {
                        case 'n': out += '\n'; break;
                        case 't': out += '\t'; break;
                        case 'r': out += '\r'; break;
                        case '"': out += '"'; break;
                        case '\\': out += '\\'; break;
                        case '/': out += '/'; break;
                        case 'u':
                            if (p + 4 < json.size()) {
                                const std::string hex = json.substr(p + 1, 4);
                                const long cp = std::strtol(hex.c_str(), nullptr, 16);
                                if (cp > 0 && cp < 128) out += static_cast<char>(cp);
                                p += 4;
                            }
                            break;
                        default: break;
                    }
                } else {
                    out += json[p];
                }
                ++p;
            }
            value = out;
            return true;
        }
        pos = json.find(needle, pos + needle.size());
    }
    return false;
}

std::string Lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

// Which bundled style does this prompt smell like?
const char* MatchStyleKey(const std::string& prompt) {
    const std::string p = Lower(prompt);
    if (p.find("halloween") != std::string::npos || p.find("spooky") != std::string::npos ||
        p.find("scary") != std::string::npos || p.find("zombie") != std::string::npos) {
        return "halloween";
    }
    if (p.find("winter") != std::string::npos || p.find("ice") != std::string::npos ||
        p.find("snow") != std::string::npos || p.find("frost") != std::string::npos) {
        return "winter";
    }
    if (p.find("christmas") != std::string::npos || p.find("santa") != std::string::npos ||
        p.find("xmas") != std::string::npos || p.find("holiday") != std::string::npos) {
        return "christmas";
    }
    if (p.find("synthwave") != std::string::npos || p.find("retro") != std::string::npos ||
        p.find("80s") != std::string::npos || p.find("neon") != std::string::npos) {
        return "synthwave";
    }
    if (p.find("edm") != std::string::npos || p.find("techno") != std::string::npos ||
        p.find("club") != std::string::npos || p.find("drop") != std::string::npos ||
        p.find("dance") != std::string::npos) {
        return "electro";
    }
    if (p.find("gold") != std::string::npos || p.find("elegant") != std::string::npos ||
        p.find("classy") != std::string::npos || p.find("formal") != std::string::npos) {
        return "gold";
    }
    if (p.find("rainbow") != std::string::npos || p.find("colorful") != std::string::npos ||
        p.find("colourful") != std::string::npos || p.find("pop") != std::string::npos) {
        return "rainbow";
    }
    if (p.find("space") != std::string::npos || p.find("meteor") != std::string::npos ||
        p.find("star") != std::string::npos || p.find("galaxy") != std::string::npos) {
        return "space";
    }
    if (p.find("candy") != std::string::npos || p.find("fast") != std::string::npos ||
        p.find("chase") != std::string::npos) {
        return "candy";
    }
    return "classic";
}

struct BuiltinStyle {
    const char* id;
    const char* name;
    const char* description;
    std::vector<std::pair<const char*, const char*>> colors;  // hex, name
};

const BuiltinStyle& BuiltinStyleFor(const std::string& prompt) {
    static const BuiltinStyle kStyles[] = {
        {"halloween", "Spooky Halloween",
         "Orange, purple and toxic green creeping meteors with shockwave accents.",
         {{"#FF8C1A", "Pumpkin"}, {"#A11AFF", "Violet"}, {"#1AFF5A", "Toxic Green"},
          {"#1A1A1A", "Night"}, {"#E66A00", "Ember"}, {"#7500CC", "Deep Purple"},
          {"#0E9E45", "Swamp"}, {"#FFB366", "Glow"}}},
        {"winter", "Winter Wonderland",
         "Icy blues and silver snowfall with slow waves and sparkle choruses.",
         {{"#1A6BFF", "Ice Blue"}, {"#B3CCFF", "Frost"}, {"#FFFFFF", "Snow"},
          {"#79AFFF", "Sky"}, {"#0B3FA8", "Deep Ice"}, {"#D9E6FF", "Mist"},
          {"#C7C7C7", "Silver"}, {"#1AE5FF", "Cyan"}}},
        {"christmas", "Christmas Classic",
         "Traditional reds and greens with gold downbeat accents and twinkle bed.",
         {{"#FF1A1A", "Santa Red"}, {"#1AFF5A", "Pine Green"}, {"#FFD700", "Gold"},
          {"#FFFFFF", "Snow White"}, {"#CC0000", "Crimson"}, {"#0E9E45", "Evergreen"},
          {"#FFE766", "Champagne"}, {"#F2F2F2", "Soft White"}}},
        {"synthwave", "Synthwave Retro",
         "80s neon magenta, cyan and purple butterflies with retro strobes.",
         {{"#FF1AE5", "Magenta"}, {"#1AE5FF", "Cyan"}, {"#A11AFF", "Purple"},
          {"#FFFFFF", "White"}, {"#CC00B8", "Hot Pink"}, {"#00B8CC", "Teal"},
          {"#7500CC", "Ultra"}, {"#FF80F0", "Pink"}}},
        {"electro", "Electro Drop",
         "Club energy: strobes, shockwaves and fast circles locked to the beat.",
         {{"#FFFFFF", "Flash"}, {"#1AE5FF", "Cyan"}, {"#FF1A1A", "Red"},
          {"#1A6BFF", "Blue"}, {"#C7C7C7", "Steel"}, {"#00B8CC", "Teal"},
          {"#FF4D4D", "Coral"}, {"#79AFFF", "Sky"}}},
        {"gold", "Elegant Gold",
         "Champagne and amber slow washes with delicate garland outlines.",
         {{"#FFD700", "Gold"}, {"#FFE766", "Champagne"}, {"#FFFFFF", "Ivory"},
          {"#E6B800", "Amber"}, {"#B38600", "Bronze"}, {"#F2F2F2", "Pearl"},
          {"#FFDF4D", "Light Gold"}, {"#CC9F00", "Old Gold"}}},
        {"rainbow", "Rainbow Pop",
         "Bright saturated rainbow -- butterflies, marquees and shimmer.",
         {{"#FF1A1A", "Red"}, {"#FF8C1A", "Orange"}, {"#FFE81A", "Yellow"},
          {"#1AFF5A", "Green"}, {"#1AE5FF", "Blue"}, {"#1A6BFF", "Indigo"},
          {"#A11AFF", "Violet"}, {"#FF1AE5", "Magenta"}}},
        {"space", "Meteor Storm",
         "Deep space blues with meteor showers and starfield twinkle.",
         {{"#0B3FA8", "Deep Blue"}, {"#FFFFFF", "Star"}, {"#1A6BFF", "Blue"},
          {"#1AE5FF", "Ice"}, {"#003080", "Navy"}, {"#C7C7C7", "Silver"},
          {"#79AFFF", "Sky"}, {"#001F66", "Abyss"}}},
        {"candy", "Candy Cane Rush",
         "Fast red-and-white stripes racing to every beat.",
         {{"#FF1A1A", "Red"}, {"#FFFFFF", "White"}, {"#CC0000", "Crimson"},
          {"#FF9999", "Blush"}, {"#FF4D4D", "Candy"}, {"#F2F2F2", "Snow"},
          {"#B30000", "Deep Red"}, {"#E6E6E6", "Silver White"}}},
        {"classic", "Classic Chase",
         "The dependable all-rounder: multi-colour chases on the beats with a "
         "twinkle bed -- works on any song, any layout.",
         {{"#FF1A1A", "Red"}, {"#FFE81A", "Yellow"}, {"#1AFF5A", "Green"},
          {"#1A6BFF", "Blue"}, {"#FFFFFF", "White"}, {"#FF1AE5", "Magenta"},
          {"#1AE5FF", "Cyan"}, {"#FF8C1A", "Orange"}}},
    };
    const std::string key = MatchStyleKey(prompt);
    for (const auto& s : kStyles) {
        if (key == s.id) return s;
    }
    return kStyles[0];
}

std::string SettingsFilePath() {
    namespace fs = std::filesystem;
    std::string base;
#ifdef _WIN32
    const char* appdata = std::getenv("APPDATA");
    base = appdata != nullptr ? appdata : ".";
#elif defined(__APPLE__)
    const char* home = std::getenv("HOME");
    base = std::string(home != nullptr ? home : ".") +
           "/Library/Application Support";
#else
    const char* xdg = std::getenv("XDG_CONFIG_HOME");
    if (xdg != nullptr && *xdg != '\0') {
        base = xdg;
    } else {
        const char* home = std::getenv("HOME");
        base = std::string(home != nullptr ? home : ".") + "/.config";
    }
#endif
    return (fs::path(base) / "xlights-ai-agent" / "AIAutoSequencer.conf").string();
}

}  // namespace

// ---------------------------------------------------------------------------
// Service
// ---------------------------------------------------------------------------

AIAutoSequencerService::AIAutoSequencerService(ServiceManager* sm) : aiBase(sm) {
    LoadSettings();
}

std::string AIAutoSequencerService::GetLLMName() const {
    return "AI Auto Sequencer";
}

std::list<aiType::TYPE> AIAutoSequencerService::GetTypes() const {
    return {aiType::PROMPT, aiType::COLORPALETTES};
}

bool AIAutoSequencerService::IsAvailable() const {
    if (AgentPing()) return true;
    return _builtinFallback;
}

bool AIAutoSequencerService::AgentPing() const {
    UrlParts url;
    if (!ParseHttpUrl(_agentUrl, url)) return false;
    url.path = "/agent/ping";
    std::string body;
    return HttpExchange(url, "GET", "", 1200, body);
}

bool AIAutoSequencerService::AgentPost(const std::string& path,
                                       const std::string& jsonBody,
                                       std::string& responseBody) const {
    UrlParts url;
    if (!ParseHttpUrl(_agentUrl, url)) return false;
    url.path = path;
    return HttpExchange(url, "POST", jsonBody, _timeoutMs, responseBody);
}

std::pair<std::string, bool> AIAutoSequencerService::CallLLM(const std::string& prompt) const {
    std::string body = "{\"prompt\": \"" + JsonEscape(prompt) + "\"}";
    std::string response;
    if (AgentPost("/v1/prompt", body, response)) {
        std::string text;
        if (ExtractJsonString(response, "text", text) && !text.empty()) {
            return {text, true};
        }
    }
    if (_builtinFallback) {
        return {BuiltinSuggestionText(prompt), true};
    }
    return {"xlights-ai agent is not running at " + _agentUrl +
            ". Start it with `xlights-ai serve` (or enable the built-in "
            "fallback in Preferences -> Services).",
            false};
}

aiBase::AIColorPalette AIAutoSequencerService::GenerateColorPalette(
    const std::string& prompt) const {
    std::string body = "{\"prompt\": \"" + JsonEscape(prompt) + "\"}";
    std::string response;
    if (AgentPost("/v1/palette", body, response)) {
        AIColorPalette palette;
        ExtractJsonString(response, "description", palette.description);
        // Collect every {hexValue, name, description} triple in order.
        size_t pos = 0;
        while ((pos = response.find("\"hexValue\"", pos)) != std::string::npos) {
            AIColor color;
            const auto end = response.find('}', pos);
            const std::string slice = response.substr(pos,
                end == std::string::npos ? std::string::npos : end - pos);
            if (ExtractJsonString(slice, "hexValue", color.hexValue)) {
                ExtractJsonString(slice, "name", color.name);
                ExtractJsonString(slice, "description", color.description);
                palette.colors.push_back(color);
            }
            pos += 10;
        }
        if (!palette.colors.empty()) {
            if (palette.description.empty()) palette.description = prompt;
            return palette;
        }
    }
    if (_builtinFallback) {
        return BuiltinPalette(prompt);
    }
    AIColorPalette err;
    err.error = "xlights-ai agent is not running at " + _agentUrl;
    return err;
}

std::vector<ServiceProperty> AIAutoSequencerService::GetProperties() const {
    std::vector<ServiceProperty> props;

    ServiceProperty cat;
    cat.kind = ServiceProperty::Kind::Category;
    cat.id = "AIAutoSequencer.Connection";
    cat.label = "Local agent";
    cat.category = "Connection";
    props.push_back(cat);

    ServiceProperty url;
    url.kind = ServiceProperty::Kind::String;
    url.id = "AIAutoSequencer.AgentUrl";
    url.label = "Agent URL";
    url.category = "Connection";
    url.help = "Base URL of the `xlights-ai serve` agent that performs beat "
               "analysis and style suggestions (default http://127.0.0.1:8756).";
    url.value = _agentUrl;
    props.push_back(url);

    ServiceProperty timeout;
    timeout.kind = ServiceProperty::Kind::Int;
    timeout.id = "AIAutoSequencer.TimeoutMs";
    timeout.label = "Request timeout (ms)";
    timeout.category = "Connection";
    timeout.help = "How long to wait for the local agent before falling back.";
    timeout.value = _timeoutMs;
    props.push_back(timeout);

    ServiceProperty cat2;
    cat2.kind = ServiceProperty::Kind::Category;
    cat2.id = "AIAutoSequencer.Capabilities";
    cat2.label = "Capabilities";
    cat2.category = "Capabilities";
    props.push_back(cat2);

    ServiceProperty p1;
    p1.kind = ServiceProperty::Kind::Bool;
    p1.id = "AIAutoSequencer.EnablePrompt";
    p1.label = "Enable Prompt";
    p1.category = "Capabilities";
    p1.help = "Answer style/sequencing prompts from the AI Auto Sequencer.";
    p1.value = _enablePrompt;
    props.push_back(p1);

    ServiceProperty p2;
    p2.kind = ServiceProperty::Kind::Bool;
    p2.id = "AIAutoSequencer.EnableColorPalette";
    p2.label = "Enable Color Palette";
    p2.category = "Capabilities";
    p2.help = "Suggest 8-colour palettes from prompts.";
    p2.value = _enablePalette;
    props.push_back(p2);

    ServiceProperty p3;
    p3.kind = ServiceProperty::Kind::Bool;
    p3.id = "AIAutoSequencer.BuiltinFallback";
    p3.label = "Built-in offline fallback";
    p3.category = "Capabilities";
    p3.help = "Answer with a small built-in keyword style engine when the "
              "xlights-ai agent is not running.";
    p3.value = _builtinFallback;
    props.push_back(p3);

    return props;
}

void AIAutoSequencerService::SetProperty(const std::string& id, bool value) {
    if (id == "AIAutoSequencer.EnablePrompt") {
        _enablePrompt = value;
        SetEnabledForType(aiType::PROMPT, value && _enablePrompt);
    } else if (id == "AIAutoSequencer.EnableColorPalette") {
        _enablePalette = value;
        SetEnabledForType(aiType::COLORPALETTES, value && _enablePalette);
    } else if (id == "AIAutoSequencer.BuiltinFallback") {
        _builtinFallback = value;
    }
}

void AIAutoSequencerService::SetProperty(const std::string& id, int value) {
    if (id == "AIAutoSequencer.TimeoutMs") {
        _timeoutMs = std::max(500, std::min(120000, value));
    }
}

void AIAutoSequencerService::SetProperty(const std::string& id, const std::string& value) {
    if (id == "AIAutoSequencer.AgentUrl") {
        std::string url = value;
        while (!url.empty() && (url.back() == '/' || url.back() == ' ')) url.pop_back();
        if (!url.empty()) _agentUrl = url;
    }
}

void AIAutoSequencerService::LoadSettings() {
    std::ifstream in(SettingsFilePath());
    std::string line;
    while (std::getline(in, line)) {
        const auto eq = line.find('=');
        if (eq == std::string::npos) continue;
        const std::string key = line.substr(0, eq);
        const std::string val = line.substr(eq + 1);
        if (key == "agentUrl" && !val.empty()) _agentUrl = val;
        else if (key == "enablePrompt") _enablePrompt = (val == "1" || val == "true");
        else if (key == "enablePalette") _enablePalette = (val == "1" || val == "true");
        else if (key == "builtinFallback") _builtinFallback = (val == "1" || val == "true");
        else if (key == "timeoutMs") _timeoutMs = std::atoi(val.c_str());
    }
    if (_timeoutMs <= 0) _timeoutMs = 8000;
    SetEnabledForType(aiType::PROMPT, _enablePrompt);
    SetEnabledForType(aiType::COLORPALETTES, _enablePalette);
}

void AIAutoSequencerService::SaveSettings() const {
    const std::string path = SettingsFilePath();
    std::error_code ec;
    std::filesystem::create_directories(
        std::filesystem::path(path).parent_path(), ec);
    std::ofstream out(path, std::ios::trunc);
    if (!out.is_open()) return;
    out << "agentUrl=" << _agentUrl << "\n"
        << "enablePrompt=" << (_enablePrompt ? "1" : "0") << "\n"
        << "enablePalette=" << (_enablePalette ? "1" : "0") << "\n"
        << "builtinFallback=" << (_builtinFallback ? "1" : "0") << "\n"
        << "timeoutMs=" << _timeoutMs << "\n";
}

std::string AIAutoSequencerService::BuiltinSuggestionText(const std::string& prompt) {
    const BuiltinStyle& style = BuiltinStyleFor(prompt);
    std::ostringstream ss;
    ss << "Style: " << style.name << " (" << style.id << ")\n"
       << style.description << "\n"
       << "Palette:";
    for (const auto& c : style.colors) ss << ' ' << c.first;
    ss << "\nPlan:\n"
       << "  [background] Twinkle / ColorWash per section on trees & matrices\n"
       << "  [outline]    Marquee per beat on house outlines & arches\n"
       << "  [accent]     Shockwave on downbeats on stars & signs\n"
       << "  [detail]     Shimmer on choruses (layer 1)\n"
       << "\n(This is the offline built-in answer. Start the full agent with "
       << "`xlights-ai serve` for beat-accurate auto-sequencing, LLM-backed "
       << "suggestions and prompt parsing -- then run `xlights-ai sequence "
       << "<song>` to sequence the current layout.)";
    return ss.str();
}

aiBase::AIColorPalette AIAutoSequencerService::BuiltinPalette(const std::string& prompt) {
    const BuiltinStyle& style = BuiltinStyleFor(prompt);
    AIColorPalette palette;
    palette.description = std::string(style.name) + ": " + style.description;
    int i = 1;
    for (const auto& c : style.colors) {
        AIColor color;
        color.hexValue = c.first;
        color.name = c.second;
        color.description = std::string(style.name) + " colour " + std::to_string(i++);
        palette.colors.push_back(color);
    }
    return palette;
}

// Generate the two C entry points xLights' ServiceManager looks for.
XL_AI_PLUGIN_EXPORT(AIAutoSequencerService)
