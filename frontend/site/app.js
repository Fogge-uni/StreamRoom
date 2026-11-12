const API_URL = "/api";
const TOKEN_KEY = "streamroom_token";

let accessToken = localStorage.getItem(TOKEN_KEY) || "";
let currentUser = null;
let noticeTimer = null;
let currentChannel = null;
let channelRequestId = 0;
let channelFormDirty = false;
let configuredOwncastUrl = "";
let chatMessages = [];
let chatRefreshInProgress = false;

const element = (id) => document.getElementById(id);

function showNotice(message, isError = false) {
    const notice = element("notice");
    notice.textContent = message;
    notice.classList.toggle("notice-error", isError);
    notice.classList.add("visible");

    if (noticeTimer) {
        clearTimeout(noticeTimer);
    }
    noticeTimer = setTimeout(() => notice.classList.remove("visible"), 3500);
}

function getErrorMessage(data) {
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
        return data.detail.map((item) => item.msg || "Проверь введённые данные").join("; ");
    }
    return "Не удалось выполнить запрос. Проверь, запущены ли контейнеры.";
}

async function apiRequest(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

    let response;
    try {
        response = await fetch(`${API_URL}${path}`, { ...options, headers });
    } catch (error) {
        throw new Error("Сервер недоступен. Повторите попытку позже.");
    }

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        if (response.status === 401 && accessToken) {
            clearAuthentication("Сеанс завершён. Выполните вход снова.");
        }
        throw new Error(getErrorMessage(data));
    }
    return data;
}

function updateAuthenticationView() {
    const loggedIn = Boolean(currentUser);
    element("auth-state").textContent = loggedIn
        ? `Вошёл: ${currentUser.username}`
        : "Не выполнен вход";
    element("account-status").textContent = loggedIn
        ? currentUser.is_channel_owner
            ? `Вход выполнен: ${currentUser.username} (владелец).`
            : `Вход выполнен: ${currentUser.username}.`
        : "Для просмотра вход не требуется.";
    element("logout").disabled = !loggedIn;
    element("channel-edit").hidden = !loggedIn || !currentUser.is_channel_owner;
    element("transfer-ownership").hidden = !loggedIn || !currentUser.is_channel_owner;
    element("chat-input").disabled = !loggedIn;
    element("chat-send").disabled = !loggedIn;
    element("chat-hint").textContent = loggedIn
        ? `Сообщение будет отправлено от имени ${currentUser.username}.`
        : "Читать чат можно без входа; для отправки войдите в аккаунт.";
    if (!loggedIn || !currentUser.is_channel_owner) {
        channelFormDirty = false;
    } else if (currentChannel && !channelFormDirty) {
        fillChannelEditForm(currentChannel);
    }
    renderChatMessages(chatMessages);
}

function clearAuthentication(message) {
    accessToken = "";
    currentUser = null;
    localStorage.removeItem(TOKEN_KEY);
    updateAuthenticationView();
    if (message) element("account-status").textContent = message;
}

async function authenticate(path) {
    const username = element("username").value.trim();
    const password = element("password").value;

    if (!username || !password) {
        showNotice("Введите имя пользователя и пароль.", true);
        return;
    }

    try {
        const result = await apiRequest(path, {
            method: "POST",
            body: JSON.stringify({ username, password }),
        });
        if (currentUser?.username !== result.username) {
            channelFormDirty = false;
        }
        accessToken = result.access_token;
        localStorage.setItem(TOKEN_KEY, accessToken);
        currentUser = {
            id: result.id,
            username: result.username,
            is_channel_owner: result.is_channel_owner,
        };
        updateAuthenticationView();
        element("password").value = "";
        showNotice(result.message || "Готово");
    } catch (error) {
        showNotice(error.message, true);
    }
}

async function logout() {
    try {
        if (accessToken) await apiRequest("/auth/logout", { method: "POST" });
        showNotice("Выход выполнен.");
    } catch (error) {
        showNotice(error.message, true);
    } finally {
        clearAuthentication();
    }
}

function setChannelState(state) {
    const pill = element("channel-state");
    pill.classList.remove("status-live", "status-offline", "status-unknown");

    if (state === "live") {
        pill.textContent = "В эфире";
        pill.classList.add("status-live");
        element("channel-message").textContent = "Трансляция доступна.";
    } else if (state === "offline") {
        pill.textContent = "Не в эфире";
        pill.classList.add("status-offline");
        element("channel-message").textContent = "Сейчас трансляция не ведётся.";
    } else {
        pill.textContent = "Нет связи";
        pill.classList.add("status-unknown");
        element("channel-message").textContent = "Не удалось получить статус канала.";
    }
}

function fillChannelEditForm(channel) {
    element("edit-channel-title").value = channel.title || "";
    element("edit-channel-description").value = channel.description || "";
}

function renderChannel(channel) {
    currentChannel = channel;
    element("channel-title").textContent = channel.title;
    element("channel-description").textContent = channel.description || "Описание канала пока не добавлено.";
    element("channel-owner").textContent = channel.owner;
    element("viewer-count").textContent = channel.server_available ? String(channel.viewer_count) : "—";
    element("live-title").textContent = channel.live_title || "Нет активного эфира";
    element("watch-link").href = channel.watch_url;
    const owncastBaseUrl = String(channel.watch_url || "").replace(/\/+$/, "");
    if (owncastBaseUrl && owncastBaseUrl !== configuredOwncastUrl) {
        element("video-frame").src = `${owncastBaseUrl}/embed/video`;
        configuredOwncastUrl = owncastBaseUrl;
    }
    setChannelState(channel.state);
    element("status-updated").textContent = `Обновлено: ${new Date().toLocaleTimeString("ru-RU")}. Автообновление каждые 15 секунд.`;
    const isOwner = currentUser?.username === channel.owner;
    if (currentUser && currentUser.is_channel_owner !== isOwner) {
        currentUser.is_channel_owner = isOwner;
        updateAuthenticationView();
    } else if (isOwner && !channelFormDirty) {
        fillChannelEditForm(channel);
    }
}

async function loadChannel(showError = true) {
    const requestId = ++channelRequestId;
    const refreshButton = element("refresh-channel");
    refreshButton.disabled = true;
    try {
        const channel = await apiRequest("/channel");
        if (requestId === channelRequestId) renderChannel(channel);
    } catch (error) {
        if (requestId !== channelRequestId) return;
        if (showError) showNotice(error.message, true);
        setChannelState("unavailable");
    } finally {
        if (requestId === channelRequestId) refreshButton.disabled = false;
    }
}

async function saveChannel(event) {
    event.preventDefault();
    const saveButton = element("save-channel");
    const titleInput = element("edit-channel-title");
    const descriptionInput = element("edit-channel-description");
    const title = titleInput.value;
    const description = descriptionInput.value;
    const sessionToken = accessToken;
    saveButton.disabled = true;
    try {
        await apiRequest("/channel", {
            method: "PUT",
            body: JSON.stringify({
                title: title.trim(),
                description: description.trim(),
            }),
        });
        if (accessToken === sessionToken && titleInput.value === title && descriptionInput.value === description) {
            channelFormDirty = false;
        }
        await loadChannel(false);
        showNotice("Настройки канала сохранены");
    } catch (error) {
        showNotice(error.message, true);
    } finally {
        saveButton.disabled = false;
    }
}

async function restoreSession() {
    if (!accessToken) {
        updateAuthenticationView();
        return;
    }

    try {
        currentUser = await apiRequest("/auth/me");
    } catch (error) {
        clearAuthentication("Сеанс недействителен. Выполните вход снова.");
        return;
    }
    updateAuthenticationView();
}

function renderChatMessages(messages) {
    const container = element("chat-messages");
    const wasNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 50;
    container.replaceChildren();

    if (!messages.length) {
        const empty = document.createElement("p");
        empty.className = "chat-empty";
        empty.textContent = "Пока нет сообщений. Начните общение!";
        container.append(empty);
    }

    for (const message of messages) {
        const item = document.createElement("article");
        item.className = "chat-message";

        const header = document.createElement("div");
        header.className = "chat-message-header";
        const username = document.createElement("strong");
        username.textContent = message.username;
        const time = document.createElement("time");
        const date = new Date(message.created_at);
        time.textContent = Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
        header.append(username, time);

        const body = document.createElement("p");
        body.textContent = message.message;
        item.append(header, body);

        if (currentUser && currentUser.is_channel_owner) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "chat-delete";
            remove.textContent = "Удалить";
            remove.addEventListener("click", () => deleteChatMessage(message.id));
            item.append(remove);
        }
        container.append(item);
    }

    element("chat-count").textContent = `Сообщений: ${messages.length}`;
    if (wasNearBottom) container.scrollTop = container.scrollHeight;
}

async function loadChat() {
    if (chatRefreshInProgress) return;
    chatRefreshInProgress = true;
    try {
        const result = await apiRequest("/chat/messages");
        chatMessages = result.messages || [];
        renderChatMessages(chatMessages);
    } catch (error) {
        element("chat-messages").textContent = "Не удалось загрузить сообщения.";
    } finally {
        chatRefreshInProgress = false;
    }
}

async function sendChatMessage(event) {
    event.preventDefault();
    const input = element("chat-input");
    const button = element("chat-send");
    const message = input.value.trim();
    if (!message) return;
    button.disabled = true;
    try {
        await apiRequest("/chat/messages", {
            method: "POST",
            body: JSON.stringify({ message }),
        });
        input.value = "";
        await loadChat();
    } catch (error) {
        showNotice(error.message, true);
    } finally {
        button.disabled = !currentUser;
    }
}

async function deleteChatMessage(messageId) {
    try {
        await apiRequest(`/chat/messages/${messageId}`, { method: "DELETE" });
        await loadChat();
        showNotice("Сообщение удалено");
    } catch (error) {
        showNotice(error.message, true);
    }
}

async function transferOwnership(event) {
    event.preventDefault();
    const button = element("transfer-button");
    const username = element("transfer-username").value.trim();
    if (!username) return;
    if (!window.confirm(`Передать права владельца пользователю ${username}? После передачи вы потеряете права владельца.`)) return;
    button.disabled = true;
    try {
        const result = await apiRequest("/channel/transfer-ownership", {
            method: "POST",
            body: JSON.stringify({ username }),
        });
        currentUser = await apiRequest("/auth/me");
        updateAuthenticationView();
        await loadChannel(false);
        showNotice(result.message);
        element("transfer-username").value = "";
    } catch (error) {
        showNotice(error.message, true);
    } finally {
        button.disabled = false;
    }
}

async function startPage() {
    updateAuthenticationView();
    await Promise.all([restoreSession(), loadChannel(false)]);
    await loadChat();
}

element("register").addEventListener("click", () => authenticate("/auth/register"));
element("login").addEventListener("click", () => authenticate("/auth/login"));
element("logout").addEventListener("click", logout);
element("refresh-channel").addEventListener("click", () => loadChannel());
element("channel-edit").addEventListener("input", () => { channelFormDirty = true; });
element("channel-edit").addEventListener("submit", saveChannel);
element("chat-form").addEventListener("submit", sendChatMessage);
element("transfer-ownership").addEventListener("submit", transferOwnership);

startPage();
window.setInterval(() => loadChannel(false), 15000);
window.setInterval(loadChat, 2500);
