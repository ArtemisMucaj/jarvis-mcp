import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.dismiss) var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Form {
                Section("Server") {
                    LabeledContent("Port") {
                        TextField("Port", value: $state.port, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                            .multilineTextAlignment(.trailing)
                    }
                    Toggle("Code Mode", isOn: $state.codeMode)
                    if state.codeMode {
                        Text("The LLM writes sandboxed Python scripts to batch tool calls instead of calling tools one at a time.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)

            HStack {
                Spacer()
                Button("Done") {
                    state.saveConfig()
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.return)
            }
            .padding()
        }
        .frame(width: 460)
        .navigationTitle("Settings")
    }
}
