async function sendMessage() {
  const input = document.getElementById("user-input");
  const message = input.value.trim();
  if (!message) return;

  addMessage("user", message);
  input.value = "";
  input.focus();

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });

    const data = await response.json();

    // Check if the response is "waiting for response..."
    if (data.reply === "⏳ Waiting for response...") {
      // Show waiting message
      addMessage("assistant", "⏳ Waiting for response...");
    } else {
      // Otherwise, show the usual reply
      addMessage("assistant", data.reply);
    }

  } catch (error) {
    console.error("Error:", error);
    addMessage("assistant", "⚠️ Oops! Something went wrong. Please try again.");
  }
}

function addMessage(sender, text) {
  const chat = document.getElementById("chat");
  const msgDiv = document.createElement("div");
  msgDiv.className = `message ${sender}`;
  msgDiv.textContent = text;
  chat.appendChild(msgDiv);
  chat.scrollTop = chat.scrollHeight;
}

// 🔑 Enter key support
document.getElementById("user-input").addEventListener("keypress", function (e) {
  if (e.key === "Enter") {
    sendMessage();
  }
});
