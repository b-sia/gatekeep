/**
 * Chat demo - communicates with the demo API which proxies to Gatekeep
 */

const messagesContainer = document.getElementById("messages");
const messageInput = document.getElementById("messageInput");
const sendBtn = document.getElementById("sendBtn");
const modelSelect = document.getElementById("model");
const streamingCheckbox = document.getElementById("streaming");

let isLoading = false;

function addMessage(content, role = "assistant", isLoading = false) {
  const messageDiv = document.createElement("div");
  messageDiv.className = `message ${role}`;
  if (isLoading) messageDiv.classList.add("loading");

  if (isLoading) {
    messageDiv.innerHTML = `
      <div class="message-content">
        <div class="typing-indicator">
          <span></span>
          <span></span>
          <span></span>
        </div>
      </div>
    `;
  } else {
    const contentDiv = document.createElement("div");
    contentDiv.className = "message-content";
    contentDiv.textContent = content;
    messageDiv.appendChild(contentDiv);
  }

  messagesContainer.appendChild(messageDiv);
  scrollToBottom();
  return messageDiv;
}

function scrollToBottom() {
  messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

function removeLoadingMessage() {
  const loading = messagesContainer.querySelector(".message.loading");
  if (loading) {
    loading.remove();
  }
}

async function sendMessage() {
  const content = messageInput.value.trim();
  if (!content || isLoading) return;

  isLoading = true;
  sendBtn.disabled = true;

  // Add user message
  addMessage(content, "user");
  messageInput.value = "";
  messageInput.style.height = "auto";

  // Add loading indicator
  const loadingMsg = addMessage("", "assistant", true);

  try {
    const model = modelSelect.value;
    const useStreaming = streamingCheckbox.checked;

    if (useStreaming) {
      await streamResponse(model, content, loadingMsg);
    } else {
      await syncResponse(model, content, loadingMsg);
    }
  } catch (error) {
    removeLoadingMessage();
    addMessage(`Error: ${error.message}`, "error");
  } finally {
    isLoading = false;
    sendBtn.disabled = false;
    messageInput.focus();
  }
}

async function streamResponse(model, content, loadingMsg) {
  removeLoadingMessage();
  const messageDiv = addMessage("", "assistant");
  const contentDiv = messageDiv.querySelector(".message-content");
  let buffer = "";

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ content, model }),
    });

    if (!response.ok) {
      throw new Error(`API error: ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line.startsWith("data: ")) {
          const json = line.substring(6);
          if (!json) continue;

          try {
            const data = JSON.parse(json);

            if (data.error) {
              contentDiv.textContent += `[Error: ${data.error}]`;
              continue;
            }

            const delta = data.choices?.[0]?.delta?.content;
            if (delta) {
              contentDiv.textContent += delta;
              scrollToBottom();
            }
          } catch (e) {
            // Skip lines that aren't valid JSON
          }
        }
      }
    }

    if (contentDiv.textContent.trim() === "") {
      contentDiv.textContent =
        "(No response received - this may happen if streaming is not properly configured)";
    }
  } catch (error) {
    messageDiv.remove();
    throw error;
  }
}

async function syncResponse(model, content, loadingMsg) {
  removeLoadingMessage();

  try {
    const response = await fetch("/api/chat-sync", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ content, model }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || `API error: ${response.status}`);
    }

    const data = await response.json();
    const responseText =
      data.choices?.[0]?.message?.content || "(No response)";
    addMessage(responseText, "assistant");
  } catch (error) {
    removeLoadingMessage();
    throw error;
  }
}

// Event listeners
sendBtn.addEventListener("click", sendMessage);

messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.shiftKey) {
    sendMessage();
  }
});

messageInput.addEventListener("input", () => {
  messageInput.style.height = "auto";
  messageInput.style.height = Math.min(messageInput.scrollHeight, 150) + "px";
});

// Focus input on load
messageInput.focus();

// Add welcome message
addMessage(
  "Hi! 👋 This is a demo of the Gatekeep gateway. Ask me anything and I'll respond using Claude via Gatekeep. Check the footer to learn how this works and how to integrate Gatekeep into your own app.",
  "assistant"
);
