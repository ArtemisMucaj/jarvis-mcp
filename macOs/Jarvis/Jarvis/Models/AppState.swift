import Foundation
import Combine
import AppKit

class AppState: ObservableObject {
    @Published var servers: [String: MCPServer] = [:]
    @Published var processManager: ProcessManager

    // Settings (persisted in UserDefaults)
    @Published var uvPath: String        { didSet { UserDefaults.standard.set(uvPath, forKey: "uvPath") } }
    @Published var port: Int             { didSet { UserDefaults.standard.set(port, forKey: "port") } }
    @Published var projectPath: String   { didSet { UserDefaults.standard.set(projectPath, forKey: "projectPath") } }
    @Published var presets: [Preset] {
        didSet { savePresets() }
    }
    @Published var activePresetID: UUID? {
        didSet { saveActivePresetID() }
    }

    var isLocalMode: Bool { !projectPath.isEmpty }

    // Auth flow
    @Published var authOutput: String = ""
    @Published var isAuthRunning = false
    
    private var cancellables = Set<AnyCancellable>()
    
    // Hardcoded GitHub URL - only this repo is allowed
    let githubURL = "https://github.com/ArtemisMucaj/jarvis-mcp"

    // Default config location
    private var defaultConfigURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".jarvis")
            .appendingPathComponent("servers.json")
    }
    
    private var configURL: URL {
        defaultConfigURL
    }

    init() {
        let savedUV      = UserDefaults.standard.string(forKey: "uvPath") ?? ProcessManager.detectUVPath()
        let savedPort    = UserDefaults.standard.integer(forKey: "port")
        let savedProject = UserDefaults.standard.string(forKey: "projectPath") ?? ""

        self.uvPath         = savedUV
        let port = (1024...65535).contains(savedPort) ? savedPort : 7070
        self.port           = port
        self.projectPath    = savedProject
        self.processManager = ProcessManager(port: port)
        self.presets = []
        self.activePresetID = nil

        // Always try to load config from default location
        loadConfig()
        
        // CRITICAL: Forward ProcessManager changes to AppState so UI updates
        processManager.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async {
                self?.objectWillChange.send()
            }
        }
        .store(in: &cancellables)

        self.presets = loadPresets()
        self.activePresetID = loadActivePresetID()
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
        if isLocalMode {
            processManager.start(uvPath: uvPath, projectPath: projectPath)
        } else {
            processManager.startFromGitHub(uvPath: uvPath, githubURL: githubURL)
        }
    }

    func stopServer() {
        processManager.stop()
    }

    func restartServer() {
        processManager.stop()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.startServer() }
    }

    // MARK: - OAuth

    func runAuth(for serverName: String) {
        guard !isAuthRunning else { return }
        isAuthRunning = true
        authOutput = ""

        // Auth needs to be run from GitHub URL too
        DispatchQueue.global(qos: .userInitiated).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: self.uvPath)
            proc.arguments = [
                "run", "--from", "git+\(self.githubURL)",
                "jarvis",
                "--auth", serverName
            ]
            proc.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser

            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError  = pipe

            pipe.fileHandleForReading.readabilityHandler = { handle in
                if let str = String(data: handle.availableData, encoding: .utf8), !str.isEmpty {
                    DispatchQueue.main.async { self.authOutput += str }
                }
            }

            try? proc.run()
            proc.waitUntilExit()

            DispatchQueue.main.async { self.isAuthRunning = false }
        }
    }

    // MARK: - Preset Persistence

    private func savePresets() {
        if let data = try? JSONEncoder().encode(presets) {
            UserDefaults.standard.set(data, forKey: "presets")
        }
    }

    private func saveActivePresetID() {
        UserDefaults.standard.set(activePresetID?.uuidString, forKey: "activePresetID")
    }

    private func loadPresets() -> [Preset] {
        guard let data = UserDefaults.standard.data(forKey: "presets"),
              let presets = try? JSONDecoder().decode([Preset].self, from: data)
        else { return [] }
        return presets
    }

    private func loadActivePresetID() -> UUID? {
        guard let str = UserDefaults.standard.string(forKey: "activePresetID") else { return nil }
        return UUID(uuidString: str)
    }
}
