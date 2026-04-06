import Foundation
import Combine
import AppKit

struct DiscoveredTool: Identifiable {
    let name: String
    let description: String
    var id: String { name }
}

// MARK: - Private Codable helpers

private struct ToolEntry: Codable {
    let name: String
    let description: String
}

/// Shape of ~/.jarvis/presets.json (written by the Python server).
private struct PresetsFile: Codable {
    var presets: [Preset]
    var activePresetID: String?
}

/// Shape returned by GET /api/presets.
private struct PresetsResponse: Codable {
    let presets: [Preset]
    let activePresetID: String?
    let activeConfigPath: String?
}

/// Shape returned by POST /api/presets.
private struct CreatePresetResponse: Codable {
    let preset: Preset
}

// MARK: - AppState

class AppState: ObservableObject {
    @Published var servers: [String: MCPServer] = [:]
    @Published var processManager: ProcessManager
    @Published var discoveredTools: [String: [DiscoveredTool]] = [:]
    @Published var isDiscoveringTools = false
    @Published var presets: [Preset] = []
    @Published var activePresetID: UUID?

    // Settings (persisted in UserDefaults); changes auto-restart the server if running.
    @Published var port: Int {
        didSet {
            // Clamp to 1024...65534 to ensure apiPort (port + 1) never exceeds 65535
            let clamped = max(1024, min(65534, port))
            if port != clamped { port = clamped; return }
            UserDefaults.standard.set(port, forKey: "port")
            processManager.port = port
            if processManager.isRunning || processManager.isStarting { restartServer() }
        }
    }
    @Published var codeMode: Bool {
        didSet {
            UserDefaults.standard.set(codeMode, forKey: "codeMode")
            processManager.codeMode = codeMode
            if processManager.isRunning || processManager.isStarting { restartServer() }
        }
    }

    private var cancellables = Set<AnyCancellable>()
    private var fileWatcherSource: DispatchSourceFileSystemObject?
    private var fileWatcherFD: Int32 = -1
    private var reloadWorkItem: DispatchWorkItem?

    /// API port is always MCP port + 1 (matches jarvis.py _start_api_thread).
    var apiPort: Int { port + 1 }
    private var apiBase: String { "http://127.0.0.1:\(apiPort)" }

    // MARK: - Config URL

    private var defaultConfigURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".jarvis/servers.json")
    }

    /// The active config file path, derived from the local (API-synced) preset state.
    var configURL: URL {
        if let id = activePresetID,
           let preset = presets.first(where: { $0.id == id }) {
            return URL(fileURLWithPath: preset.filePath)
        }
        return defaultConfigURL
    }

    // MARK: - Init

    init() {
        let savedPort     = UserDefaults.standard.integer(forKey: "port")
        let savedCodeMode = UserDefaults.standard.bool(forKey: "codeMode")
        // Clamp to 1024...65534 to ensure apiPort (port + 1) never exceeds 65535
        let port          = (1024...65534).contains(savedPort) ? savedPort : 7070

        self.port           = port
        self.codeMode       = savedCodeMode
        self.processManager = ProcessManager(port: port, codeMode: savedCodeMode)

        // Bootstrap preset state from disk so the UI is populated before the server starts.
        let (initialPresets, initialActiveID) = AppState.loadPresetsFromDisk()
        self.presets        = initialPresets
        self.activePresetID = initialActiveID

        loadConfig()
        startFileWatcher()

        // Forward ProcessManager changes to AppState so UI updates.
        processManager.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async { self?.objectWillChange.send() }
        }
        .store(in: &cancellables)
    }

    // MARK: - Disk bootstrap (used only before the server is online)

    /// Read presets from ~/.jarvis/presets.json without going through the API.
    /// This is called once at init so the UI has data before the server starts.
    static func loadPresetsFromDisk() -> ([Preset], UUID?) {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".jarvis/presets.json")
        guard let data = try? Data(contentsOf: path),
              let file = try? JSONDecoder().decode(PresetsFile.self, from: data)
        else { return ([], nil) }
        let activeID = file.activePresetID.flatMap { UUID(uuidString: $0) }
        return (file.presets, activeID)
    }

    // MARK: - File Watcher

    /// Watches the active configURL for external modifications using kqueue.
    /// Debounces rapid writes (editors often write multiple times on save) by 0.3 s.
    private func startFileWatcher() {
        stopFileWatcher()
        let path = configURL.path
        let fd = open(path, O_EVTONLY)
        guard fd >= 0 else { print("⚠️ Could not open \(path) for watching"); return }
        fileWatcherFD = fd

        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .delete],
            queue: .global(qos: .utility)
        )
        source.setEventHandler { [weak self] in
            guard let self else { return }
            self.reloadWorkItem?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                print("🔄 Config file changed on disk, reloading…")
                DispatchQueue.main.async {
                    self.loadConfig()
                    self.startFileWatcher()
                }
            }
            self.reloadWorkItem = work
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.3, execute: work)
        }
        source.setCancelHandler { if fd >= 0 { close(fd) } }
        source.resume()
        fileWatcherSource = source
        print("👁️ Watching config file: \(path)")
    }

    private func stopFileWatcher() {
        reloadWorkItem?.cancel()
        reloadWorkItem = nil
        fileWatcherSource?.cancel()
        fileWatcherSource = nil
    }

    deinit { stopFileWatcher() }

    // MARK: - Config (local read for the servers panel)

    func loadConfig() {
        let configDir = configURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
        print("📁 Loading config from: \(configURL.path)")
        guard let data   = try? Data(contentsOf: configURL),
              let config = try? JSONDecoder().decode(ServersConfig.self, from: data)
        else {
            print("⚠️ No config found, creating default config")
            createDefaultConfig()
            return
        }
        servers = config.mcpServers
        print("✓ Loaded \(servers.count) server(s) from config")
    }

    func saveConfig() {
        let configDir = configURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
        let config  = ServersConfig(mcpServers: servers)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        guard let data = try? encoder.encode(config) else { return }
        try? data.write(to: configURL)
        print("✓ Saved config to: \(configURL.path)")
    }

    private func createDefaultConfig() {
        let sampleServers: [String: MCPServer] = [
            "example-filesystem": MCPServer(
                command: "npx",
                args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                env: nil, url: nil, transport: "stdio", auth: nil, enabled: false),
            "example-github": MCPServer(
                command: "npx",
                args: ["-y", "@modelcontextprotocol/server-github"],
                env: ["GITHUB_PERSONAL_ACCESS_TOKEN": "your-token-here"],
                url: nil, transport: "stdio", auth: nil, enabled: false)
        ]
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        guard let data = try? encoder.encode(ServersConfig(mcpServers: sampleServers)) else { return }
        try? data.write(to: configURL)
        print("✓ Created default config at: \(configURL.path)")
        servers = sampleServers
    }

    // MARK: - Process

    func startServer() { processManager.startBundled() }
    func stopServer()  { processManager.stop() }

    func restartServer() {
        processManager.stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.startServer() }
    }

    // MARK: - Tool Discovery (API only)

    func discoverTools() {
        guard !isDiscoveringTools, processManager.isRunning else { return }
        isDiscoveringTools = true

        guard let url = URL(string: "\(apiBase)/api/tools") else {
            isDiscoveringTools = false
            return
        }

        // Allow enough time for all upstream MCP servers to be probed.
        var request = URLRequest(url: url)
        request.timeoutInterval = 90

        print("🔍 Tool discovery via API: \(url)")

        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self else { return }

            if let error {
                print("🔍 Tool discovery failed: \(error.localizedDescription)")
                DispatchQueue.main.async { self.isDiscoveringTools = false }
                return
            }

            guard let data,
                  let parsed = try? JSONDecoder().decode([String: [ToolEntry]].self, from: data)
            else {
                print("🔍 Tool discovery: unexpected response")
                DispatchQueue.main.async { self.isDiscoveringTools = false }
                return
            }

            let tools = parsed.mapValues { entries in
                entries.map { DiscoveredTool(name: $0.name, description: $0.description) }
            }
            for (server, list) in tools.sorted(by: { $0.key < $1.key }) {
                print("🔍 \(server): \(list.count) tools")
            }

            DispatchQueue.main.async {
                self.discoveredTools = tools
                self.isDiscoveringTools = false
            }
        }.resume()
    }

    func isToolDisabled(server: String, tool: String) -> Bool {
        servers[server]?.disabledTools?.contains(tool) ?? false
    }

    func toggleTool(server: String, tool: String) {
        if servers[server]?.disabledTools == nil { servers[server]?.disabledTools = [] }
        if let idx = servers[server]?.disabledTools?.firstIndex(of: tool) {
            servers[server]?.disabledTools?.remove(at: idx)
        } else {
            servers[server]?.disabledTools?.append(tool)
        }
        if servers[server]?.disabledTools?.isEmpty == true { servers[server]?.disabledTools = nil }
        servers[server]?.requiresRestart = true
        saveConfig()
    }

    // MARK: - Preset API

    /// Fetch the current preset list from the server and sync local state.
    func fetchPresets() {
        guard let url = URL(string: "\(apiBase)/api/presets") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self,
                  let data,
                  let response = try? JSONDecoder().decode(PresetsResponse.self, from: data)
            else { return }
            let activeID = response.activePresetID.flatMap { UUID(uuidString: $0) }
            DispatchQueue.main.async {
                self.presets        = response.presets
                self.activePresetID = activeID
                self.loadConfig()
                self.startFileWatcher()
            }
        }.resume()
    }

    /// Add a new preset.  Calls POST /api/presets and updates local state on success.
    func addPreset(name: String, filePath: String, completion: ((Bool) -> Void)? = nil) {
        guard !filePath.isEmpty,
              let url = URL(string: "\(apiBase)/api/presets")
        else { completion?(false); return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONEncoder().encode(["name": name.isEmpty
            ? URL(fileURLWithPath: filePath).deletingPathExtension().lastPathComponent
            : name, "filePath": filePath])

        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self,
                  let data,
                  let r = try? JSONDecoder().decode(CreatePresetResponse.self, from: data)
            else { completion?(false); return }
            DispatchQueue.main.async {
                self.presets.append(r.preset)
                completion?(true)
            }
        }.resume()
    }

    /// Remove a preset.  Calls DELETE /api/presets/{id} and updates local state.
    func removePreset(_ preset: Preset, completion: ((Bool) -> Void)? = nil) {
        guard let url = URL(string: "\(apiBase)/api/presets/\(preset.id)") else {
            completion?(false); return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        let wasActive = activePresetID == preset.id

        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            guard let self,
                  let http = response as? HTTPURLResponse, http.statusCode == 200
            else { completion?(false); return }
            DispatchQueue.main.async {
                self.presets.removeAll { $0.id == preset.id }
                if wasActive {
                    self.activePresetID = nil
                    self.loadConfig()
                    self.startFileWatcher()
                    if self.processManager.isRunning || self.processManager.isStarting {
                        self.restartServer()
                    }
                }
                completion?(true)
            }
        }.resume()
    }

    /// Rename a preset.  Calls PATCH /api/presets/{id}.
    func renamePreset(id: UUID, to name: String) {
        guard let url = URL(string: "\(apiBase)/api/presets/\(id)") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "PATCH"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONEncoder().encode(["name": name])
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            guard let self else { return }

            // Decode response and update local state
            if let error {
                print("⚠️ Failed to rename preset: \(error.localizedDescription)")
                return
            }

            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                print("⚠️ Rename preset failed with non-200 response")
                return
            }

            // Decode the returned preset (server returns {"preset": {...}})
            if let data,
               let decoded = try? JSONDecoder().decode([String: Preset].self, from: data),
               let updatedPreset = decoded["preset"] {
                DispatchQueue.main.async {
                    if let idx = self.presets.firstIndex(where: { $0.id == id }) {
                        self.presets[idx] = updatedPreset
                    }
                }
            }
        }.resume()
    }

    /// Activate a preset (or nil for default).  Calls POST /api/presets/{id}/activate,
    /// updates local state, then restarts the server so it picks up the new config.
    func switchPreset(_ preset: Preset?, completion: ((Bool) -> Void)? = nil) {
        let path: String
        if let preset {
            path = "\(apiBase)/api/presets/\(preset.id)/activate"
        } else {
            path = "\(apiBase)/api/presets/default/activate"
        }
        guard let url = URL(string: path) else { completion?(false); return }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        let wasRunning = processManager.isRunning || processManager.isStarting

        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            guard let self,
                  let http = response as? HTTPURLResponse, http.statusCode == 200
            else { completion?(false); return }
            DispatchQueue.main.async {
                self.activePresetID = preset?.id
                self.loadConfig()
                self.startFileWatcher()
                if wasRunning { self.restartServer() }
                completion?(true)
            }
        }.resume()
    }
}