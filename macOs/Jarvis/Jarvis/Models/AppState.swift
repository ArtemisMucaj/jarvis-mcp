import Foundation
import Combine
import AppKit

class AppState: ObservableObject {
    @Published var servers: [String: MCPServer] = [:]
    @Published var processManager: ProcessManager

    // Settings (persisted in UserDefaults)
    @Published var port: Int             { didSet { UserDefaults.standard.set(port, forKey: "port") } }
    @Published var presets: [Preset]
    @Published var activePresetID: UUID? {
        didSet { saveActivePresetID() }
    }

    private var cancellables = Set<AnyCancellable>()
    private var fileWatcherSource: DispatchSourceFileSystemObject?
    private var fileWatcherFD: Int32 = -1
    private var reloadWorkItem: DispatchWorkItem?

    // Default config location
    private var defaultConfigURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".jarvis")
            .appendingPathComponent("servers.json")
    }
    
    /// The active config file URL: resolves to the active preset's path if one is set,
    /// otherwise falls back to the default `~/.jarvis/servers.json`.
    /// Exposed internally so `PresetsView` can display the current config path.
    var configURL: URL {
        if let id = activePresetID,
           let preset = presets.first(where: { $0.id == id }) {
            return URL(fileURLWithPath: preset.filePath)
        }
        return defaultConfigURL
    }

    init() {
        let savedPort    = UserDefaults.standard.integer(forKey: "port")

        let port = (1024...65535).contains(savedPort) ? savedPort : 7070
        self.port           = port
        self.processManager = ProcessManager(port: port)
        self.presets = AppState.loadPresets()
        self.activePresetID = AppState.loadActivePresetID()

        if let id = self.activePresetID, !self.presets.contains(where: { $0.id == id }) {
            self.activePresetID = nil
        }

        // Load config from the active preset's path (or default if none active)
        loadConfig()
        startFileWatcher()
        
        // CRITICAL: Forward ProcessManager changes to AppState so UI updates
        processManager.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async {
                self?.objectWillChange.send()
            }
        }
        .store(in: &cancellables)

        // Persist preset name edits: didSet only fires on full reassignment,
        // not on element-level mutations via @Binding. The Combine publisher
        // emits on any mutation, so this catches in-place name changes.
        $presets
            .dropFirst()
            .sink { [weak self] newPresets in self?.savePresets(newPresets) }
            .store(in: &cancellables)
    }

    // MARK: - File Watcher

    /// Watches the active `configURL` for external modifications using kqueue via GCD.
    /// Debounces rapid writes (editors often write multiple times on save) by 0.3s.
    private func startFileWatcher() {
        stopFileWatcher()

        let path = configURL.path
        let fd = open(path, O_EVTONLY)
        guard fd >= 0 else {
            print("⚠️ Could not open \(path) for watching")
            return
        }
        fileWatcherFD = fd

        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .delete],
            queue: .global(qos: .utility)
        )

        source.setEventHandler { [weak self] in
            guard let self else { return }
            // Debounce: cancel any pending reload and schedule a new one
            self.reloadWorkItem?.cancel()
            let work = DispatchWorkItem { [weak self] in
                guard let self else { return }
                print("🔄 Config file changed on disk, reloading…")
                DispatchQueue.main.async {
                    self.loadConfig()
                    // Restart watcher: the fd may now point to a stale inode
                    // (editors do atomic saves via write-tmp + rename)
                    self.startFileWatcher()
                }
            }
            self.reloadWorkItem = work
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.3, execute: work)
        }

        source.setCancelHandler {
            if fd >= 0 { close(fd) }
        }

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

    deinit {
        stopFileWatcher()
    }

    // MARK: - Config

    func loadConfig() {
        // Ensure config directory exists
        let configDir = configURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
        
        print("📁 Loading config from: \(configURL.path)")
        
        // Try to load existing config
        guard let data = try? Data(contentsOf: configURL),
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
        // Ensure config directory exists
        let configDir = configURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
        
        let config = ServersConfig(mcpServers: servers)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(config) else { return }
        try? data.write(to: configURL)
        
        print("✓ Saved config to: \(configURL.path)")
    }
    
    private func createDefaultConfig() {
        // Create a sample config file with examples
        let sampleServers: [String: MCPServer] = [
            "example-filesystem": MCPServer(
                command: "npx",
                args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                env: nil,
                url: nil,
                transport: "stdio",
                auth: nil,
                enabled: false
            ),
            "example-github": MCPServer(
                command: "npx",
                args: ["-y", "@modelcontextprotocol/server-github"],
                env: ["GITHUB_PERSONAL_ACCESS_TOKEN": "your-token-here"],
                url: nil,
                transport: "stdio",
                auth: nil,
                enabled: false
            )
        ]
        
        let defaultConfig = ServersConfig(mcpServers: sampleServers)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(defaultConfig) else { return }
        try? data.write(to: configURL)
        
        print("✓ Created default config with examples at: \(configURL.path)")
        
        // Reload to show the examples
        servers = sampleServers
    }

    // MARK: - Process

    func startServer() {
        processManager.startBundled()
    }

    func stopServer() {
        processManager.stop()
    }

    func restartServer() {
        processManager.stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.startServer() }
    }

    /// Activates the given preset, or reverts to the default config if `preset` is `nil`.
    /// Reloads the server config immediately; if the server was running it is restarted
    /// asynchronously (with a short delay — the restart may race on slow machines).
    func switchPreset(_ preset: Preset?) {
        let wasRunning = processManager.isRunning || processManager.isStarting
        activePresetID = preset?.id
        loadConfig()
        startFileWatcher() // Re-watch the new config path
        if wasRunning {
            restartServer()
        }
    }

    func addPreset(name: String, filePath: String) {
        guard !filePath.isEmpty else { return }
        let preset = Preset(name: name.isEmpty ? URL(fileURLWithPath: filePath).deletingPathExtension().lastPathComponent : name,
                            filePath: filePath)
        presets.append(preset)
    }

    func removePreset(_ preset: Preset) {
        let wasActive = activePresetID == preset.id
        presets.removeAll { $0.id == preset.id }
        if wasActive {
            switchPreset(nil)
        }
    }

    // MARK: - Preset Persistence

    private func savePresets(_ presetsToSave: [Preset]) {
        if let data = try? JSONEncoder().encode(presetsToSave) {
            UserDefaults.standard.set(data, forKey: "presets")
        }
    }

    private func saveActivePresetID() {
        UserDefaults.standard.set(activePresetID?.uuidString, forKey: "activePresetID")
    }

    private static func loadPresets() -> [Preset] {
        guard let data = UserDefaults.standard.data(forKey: "presets"),
              let presets = try? JSONDecoder().decode([Preset].self, from: data)
        else { return [] }
        return presets
    }

    private static func loadActivePresetID() -> UUID? {
        guard let str = UserDefaults.standard.string(forKey: "activePresetID") else { return nil }
        return UUID(uuidString: str)
    }
}
