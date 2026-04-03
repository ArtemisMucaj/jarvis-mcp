import SwiftUI

struct ServerDetailView: View {
    let name: String
    let server: MCPServer
    @EnvironmentObject var state: AppState

    var body: some View {
        Form {
            // Connection
            Section("Connection") {
                if let url = server.url {
                    LabeledContent("URL", value: url)
                    LabeledContent("Transport", value: server.transport ?? "http")
                } else if let command = server.command {
                    LabeledContent("Command", value: command)
                    if let args = server.args, !args.isEmpty {
                        LabeledContent("Args", value: args.joined(separator: " "))
                    }
                    LabeledContent("Transport", value: "stdio")
                }
            }

            // Environment
            if let env = server.env, !env.isEmpty {
                Section("Environment") {
                    ForEach(env.keys.sorted(), id: \.self) { key in
                        LabeledContent(key, value: env[key] ?? "")
                    }
                }
            }

            // Status
            Section("Status") {
                LabeledContent("Enabled") {
                    Toggle("", isOn: Binding(
                        get: { server.enabled ?? true },
                        set: { newValue in
                            state.servers[name]?.enabled = newValue
                            state.saveConfig()
                        }
                    ))
                    .labelsHidden()
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle(name)
        .navigationSubtitle(server.isOAuth ? "OAuth" : (server.isHTTP ? "HTTP" : "stdio"))
    }
}
