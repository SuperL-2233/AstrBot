/** Adapt OpenCode's session.idle event to the AstrBot webhook protocol. */
import os from "node:os"

export const AgentCompletionQQPlugin = async ({ client, directory }) => {
  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle") return

      const url = process.env.ASTRBOT_AGENT_WEBHOOK_URL
      const token = process.env.ASTRBOT_AGENT_WEBHOOK_TOKEN
      if (!url || !token) {
        console.error("OpenCode QQ notifier: webhook URL or token is not configured")
        return
      }

      const properties = event.properties ?? {}
      const sessionID = properties.sessionID ?? properties.sessionId ?? event.sessionID
      if (!sessionID) {
        console.error("OpenCode QQ notifier: session.idle did not contain a session ID")
        return
      }

      let message = ""
      let messageID = ""
      try {
        const response = await client.session.messages({
          path: { id: sessionID },
          query: { limit: 20 },
        })
        const messages = response.data ?? response
        const assistant = [...messages].reverse().find((item) => item.info?.role === "assistant")
        messageID = assistant?.info?.id ?? ""
        message = (assistant?.parts ?? [])
          .filter((part) => part.type === "text")
          .map((part) => part.text)
          .join("\n")
      } catch (error) {
        console.error(`OpenCode QQ notifier could not read the final message: ${error}`)
      }

      const payload = {
        source: "opencode",
        event: "session.idle",
        event_id: `opencode:${sessionID}:${messageID || Date.now()}`,
        session_id: sessionID,
        project: directory,
        host: process.env.ASTRBOT_AGENT_NOTIFY_NAME || `${os.hostname()}-OpenCode`,
        message,
      }
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify(payload),
        })
        if (!response.ok) {
          console.error(`OpenCode QQ notifier failed: HTTP ${response.status} ${await response.text()}`)
        }
      } catch (error) {
        console.error(`OpenCode QQ notifier failed: ${error}`)
      }
    },
  }
}
