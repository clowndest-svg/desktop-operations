<template>
  <div class="app" :class="{ 'app--kb': keyboardOpen, 'app--large': workspace.reading.fontSize === 'large', 'app--compact': workspace.reading.compact }">
    <div class="mobile-portrait-backdrop" aria-hidden="true"><MoonPortrait /></div>
    <div v-if="drawer" class="scrim" @click="drawer = false" />

    <aside class="drawer" :class="{ 'drawer--on': drawer }">
      <div class="drawer__head">
        <span class="drawer__brand"><img :src="brandLogo" alt="小夜" width="42" height="42" />小夜</span>
        <button class="icon" type="button" aria-label="关闭" @click="drawer = false">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          </svg>
        </button>
      </div>

      <button class="drawer__new" type="button" :disabled="!ready || busy" @click="newSession">
        <svg viewBox="0 0 24 24" width="16" height="16">
          <path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
        开一段新的
      </button>

      <template v-if="!link">
        <input v-model="sessionQuery" class="local-search" type="search" aria-label="搜索手机会话" placeholder="搜索标题和聊天内容" />
        <div class="session-filters"><button type="button" :aria-pressed="!showArchive" @click="showArchive = false">最近会话</button><button type="button" :aria-pressed="showArchive" @click="showArchive = true">归档 {{ workspace.sessions.filter(s => s.archived).length }}</button></div>
        <ul class="drawer__list">
          <li v-for="row in localSessions" :key="row.id" class="local-session">
            <button type="button" :disabled="busy" :class="{ 'is-on': row.id === workspace.active }" @click="openLocalSession(row.id)"><span class="drawer__title">{{ row.pinned ? '置顶 · ' : '' }}{{ row.title }}</span><span class="drawer__meta">{{ row.turns.length }} 条 · {{ presetName(row.preset) }}</span></button>
            <div class="local-session__tools"><button type="button" :disabled="busy" @click="pinSession(row.id)">{{ row.pinned ? '取消置顶' : '置顶' }}</button><button type="button" :disabled="busy" @click="renameSession(row.id)">重命名</button><button type="button" :disabled="busy" @click="archiveSession(row.id)">{{ row.archived ? '移出归档' : '归档' }}</button><button type="button" :disabled="busy" @click="deleteSession(row.id)">删除</button></div>
          </li>
          <li v-if="!localSessions.length" class="drawer__empty">没有找到匹配的手机会话</li>
        </ul>
      </template>
      <ul v-else class="drawer__list">
        <li v-for="row in sessions" :key="row.id">
          <button
            type="button"
            :class="{ 'is-on': row.id === currentSession }"
            @click="openSession(row.id)"
          >
            <span class="drawer__title">{{ row.title || '未命名' }}</span>
            <span class="drawer__meta">{{ row.turns }} 轮</span>
          </button>
        </li>
        <li v-if="!sessions.length" class="drawer__empty">电脑上还没有别的会话</li>
      </ul>

      <div class="drawer__foot">
        <button class="ghost" type="button" @click="drawer = false; chatSearch = !chatSearch">搜索本段聊天</button>
        <button class="ghost" type="button" @click="drawer = false; workbench = true">AI 工作台 · 收藏与备份</button>
        <button v-if="!link" class="ghost" type="button" @click="drawer = false; sheet = 'connect'">
          连电脑
        </button>
        <button class="ghost" type="button" @click="toggleAppearance">
          {{ appearance === 'light' ? '切到深色' : '切到浅色' }}
        </button>
        <span class="drawer__ver">v{{ APP_VERSION }}</span>
      </div>
    </aside>

    <header class="top">
      <button class="icon" type="button" aria-label="会话" @click="drawer = true">
        <svg viewBox="0 0 24 24" width="19" height="19">
          <path d="M4 7h16M4 12h16M4 17h10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
        </svg>
      </button>

      <div class="top__mid">
        <img class="top__logo" :src="brandLogo" alt="小夜 AI 语音助手" width="36" height="36" />
        <strong class="top__name">{{ title }}</strong>
        <span class="top__sub" :class="{ 'top__sub--live': link }">{{ subtitle }}</span>
      </div>

      <button
        class="icon"
        type="button"
        :aria-label="link ? '这台电脑' : '连电脑'"
        @click="sheet = link ? 'pc' : 'connect'"
      >
        <svg viewBox="0 0 24 24" width="19" height="19">
          <rect x="3" y="4.5" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.7" />
          <path d="M9 20h6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
        </svg>
      </button>

      <button
        class="icon icon--call"
        :class="{ 'is-waiting': !link }"
        type="button"
        :aria-label="link ? '语音通话' : '语音通话（要先连电脑）'"
        @click="openCall"
      >
        <svg viewBox="0 0 24 24" width="19" height="19">
          <path
            d="M4.5 5.5h4l1.6 4-2 1.4a11 11 0 005.6 5.6l1.4-2 4 1.6v4a1.5 1.5 0 01-1.7 1.5C10.6 20.6 3.4 13.4 3 6.2A1.5 1.5 0 014.5 5.5z"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linejoin="round"
          />
        </svg>
      </button>

      <button class="icon" type="button" aria-label="设置" @click="sheet = 'settings'">
        <svg viewBox="0 0 24 24" width="19" height="19">
          <circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.7" />
          <path
            d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2M6 6l1.4 1.4M16.6 16.6L18 18M18 6l-1.4 1.4M7.4 16.6L6 18"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
          />
        </svg>
      </button>
    </header>

    <!-- 独立模式横幅：把"连上电脑能多得到什么"一次说完，并且**可点**。
         之前它只写"能看屏幕、建提醒、查机器"，而语音通话与音色同样依赖电脑，
         那两个入口又因为没连电脑而不显示 —— 结果是"装上了但找不到语音"。
         一句话里说清，再给一个直达配对的入口，比让人自己翻设置强。 -->
    <button v-if="!link" class="banner" type="button" @click="sheet = 'connect'">
      独立模式支持聊天、拍照和 AI 工作台。<strong>语音通话和音色要连上电脑</strong>才能用 ——
      手机和电脑连同一个 Wi-Fi，在电脑上打开「手机接入」。<em>点这里去连接</em>
    </button>

    <div v-if="chatSearch" class="chat-search" role="search" aria-label="搜索本段聊天"><input v-model="messageQuery" type="search" aria-label="聊天关键词" placeholder="在本段聊天中查找" /><span aria-live="polite">{{ matches.length ? matchCursor + 1 : 0 }}/{{ matches.length }}</span><button type="button" :disabled="!matches.length" aria-label="上一个结果" @click="jumpMatch(-1)">↑</button><button type="button" :disabled="!matches.length" aria-label="下一个结果" @click="jumpMatch(1)">↓</button><button type="button" aria-label="关闭聊天搜索" @click="chatSearch = false; messageQuery = ''">×</button></div>
    <main ref="scroller" class="stream" @scroll.passive="onScroll">
      <section v-if="!turns.length" class="hello">
        <div class="hello__orb" aria-hidden="true" />
        <h1 class="hello__title">{{ link ? '她在这台电脑上等着' : '说点什么' }}</h1>
        <p class="hello__sub">
          {{
            link
              ? '你说的话走的是电脑上那个她：同一份历史、同一套权限分级。'
              : '按住下面的麦克风直接讲，或者打字。填一次自己的模型 Key 就能一直用。'
          }}
        </p>
        <div class="hello__ideas">
          <button type="button" :disabled="busy || !ready" @click="workbench = true">✦ 打开 AI 工作台</button>
          <button v-for="idea in ideas" :key="idea" type="button" :disabled="busy" @click="ask(idea)">
            {{ idea }}
          </button>
        </div>
      </section>

      <MessageBubble
        v-for="(turn, index) in turns"
        :key="index"
        :turn="shown(turn, index)"
        :streaming="index === streaming"
        :can-regenerate="!busy && turn.role === 'assistant' && hasAskBefore(index)"
        :disabled="busy"
        :data-message-index="index"
        :class="{ 'search-match': chatSearch && matches[matchCursor] === index }"
        :saved="workspace.saved.some(item => item.text === turn.text)"
        @skip="reveal.skip()"
        @speak="speakTurn(index)"
        @regenerate="regenerate(index)"
        @remove="drop(index)"
        @save="saveAnswer(index)"
        @followup="followup(index)"
        @edit="editQuestion(index)"
        @quote="quoteTurn(index)"
        @branch="branchTurn(index)"
        @note="noteFromTurn(index)"
        @shorten="transformTurn(index, 'shorten')"
        @checklist="transformTurn(index, 'checklist')"
      />

      <p v-if="busy && streaming < 0" class="thinking">
        <span class="thinking__dot" /><span class="thinking__dot" /><span class="thinking__dot" />
        {{ link ? '电脑上的她在想' : '她在想' }}
      </p>

      <!--
        卡住要看得见。上面那三个点只在**没进流式**时出现；进了流式而一个字都还没来，
        屏幕上就只是一个空气泡，跟"她死了"分不出来。这里按"多久没有新进展"报数：
        每来一段字就归零，超过 STALL_SHOW_AFTER 秒才开口。只报数，**不自动取消**——
        替用户掐掉一次问答比多等十秒贵得多。
      -->
      <p v-if="busy && stall >= STALL_SHOW_AFTER" class="thinking thinking--slow">
        <template v-if="live >= 0">
          已经 {{ stall }} 秒没有新字了。按下面的「停止」可以不等她
        </template>
        <template v-else>
          电脑上那一问已经跑了 {{ stall }} 秒。话在电脑上跑，这边按不了停
        </template>
      </p>
    </main>

    <button v-if="far" class="jump" type="button" aria-label="回到底部" @click="scroll">
      <svg viewBox="0 0 24 24" width="18" height="18">
        <path d="M12 5v13M6 13l6 6 6-6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>

    <div v-if="pendingDocument" class="document-attach"><span>资料 · {{ pendingDocument.name }} · {{ pendingDocument.text.length.toLocaleString() }} 字</span><button type="button" @click="pendingDocument = null">移除</button></div>
    <input ref="documentInput" type="file" class="hidden-file" accept=".txt,.md,.csv,.json,.log,.ts,.js,.py,.html,.css" @change="readDocument" />
    <div v-if="pending" class="attach">
      <img :src="pending.data" alt="要一起发出去的照片" />
      <span>会跟着下一句一起发出去</span>
      <button type="button" @click="pending = null">去掉</button>
    </div>

    <footer class="dock">
      <div v-if="tray" class="tray">
        <button type="button" :disabled="busy || !ready" @click="workbench = true; tray = false">✦ AI 工作台</button>
        <button type="button" :disabled="busy || !ready" @click="chooseDocument">▤ 读文件</button>
        <button type="button" :disabled="busy" @click="shoot('camera')">
          <svg viewBox="0 0 24 24" width="20" height="20">
            <path d="M4 8.5h3l1.5-2h7L17 8.5h3v10H4z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" />
            <circle cx="12" cy="13" r="3.2" fill="none" stroke="currentColor" stroke-width="1.7" />
          </svg>
          拍一张
        </button>
        <button type="button" :disabled="busy" @click="shoot('photos')">
          <svg viewBox="0 0 24 24" width="20" height="20">
            <rect x="3.5" y="5" width="17" height="14" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.7" />
            <path d="M6 16l4-4 3 3 2-2 3 3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
            <circle cx="9" cy="9.5" r="1.2" fill="currentColor" />
          </svg>
          从相册
        </button>
        <button type="button" :disabled="busy" @click="sheet = 'phone'; tray = false">
          <svg viewBox="0 0 24 24" width="20" height="20">
            <rect x="7" y="2.8" width="10" height="18.4" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.7" />
            <path d="M10.5 5.6h3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
          </svg>
          这台手机
        </button>
        <button v-if="link" type="button" :disabled="busy" @click="sheet = 'pc'; tray = false">
          <svg viewBox="0 0 24 24" width="20" height="20">
            <rect x="3" y="4.5" width="18" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.7" />
            <path d="M9 20h6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
          </svg>
          这台电脑
        </button>
        <button type="button" :class="{ 'is-waiting': !link }" @click="openVoice">
          <svg viewBox="0 0 24 24" width="20" height="20">
            <path d="M12 4v10M8.5 13.5a3.5 3.5 0 007 0" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
            <path d="M6 18h12M9 21h6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
          </svg>
          音色
        </button>
      </div>

      <div class="dock__bar">
        <button class="dock__plus" type="button" :class="{ 'is-on': tray }" aria-label="更多" @click="tray = !tray">
          <svg viewBox="0 0 24 24" width="20" height="20">
            <path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" />
          </svg>
        </button>

        <textarea
          ref="input"
          v-model="draft"
          class="dock__input"
          rows="1"
          maxlength="40000"
          enterkeyhint="send"
          :placeholder="busy ? '等她答完这一句' : '想问什么'"
          @input="grow"
          @keydown="onKey"
          @focus="scroll"
        />

        <!-- 只有流式那一趟按得动：电脑上跑着的那一问，这边按了也拦不住，
             所以不显示这个按钮，而不是显示一个"按了没用"的按钮。 -->
        <button
          v-if="live >= 0"
          class="dock__stop"
          type="button"
          aria-label="不听她说了"
          @click="stopAnswer"
        >
          <svg viewBox="0 0 24 24" width="18" height="18">
            <rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" />
          </svg>
          停止
        </button>
        <button
          v-else-if="draft.trim() || pending || pendingDocument"
          class="dock__send"
          type="button"
          :disabled="busy || !ready"
          aria-label="发送"
          @click="submit"
        >
          <svg viewBox="0 0 24 24" width="20" height="20">
            <path d="M12 19V6M6.5 11.5L12 6l5.5 5.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <button
          v-else
          class="dock__mic"
          :class="{ 'is-on': listening }"
          type="button"
          :disabled="busy || !cap.native"
          :aria-label="cap.native ? '按住说话' : cap.reason"
          @pointerdown.prevent="startTalk"
          @pointerup.prevent="stopTalk"
          @pointercancel="cancelTalk"
          @pointerleave="cancelTalk"
        >
          <svg viewBox="0 0 24 24" width="20" height="20">
            <rect x="9.2" y="3.5" width="5.6" height="10" rx="2.8" fill="none" stroke="currentColor" stroke-width="1.8" />
            <path d="M5.5 11.5a6.5 6.5 0 0013 0M12 18v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          </svg>
        </button>
      </div>

      <p v-if="!cap.native && cap.reason" class="dock__why">{{ cap.reason }}</p>
    </footer>

    <Transition name="toast">
      <p v-if="toast" class="toast" :class="{ 'toast--bad': toast.bad }" @click="toast = null">
        {{ toast.text }}
      </p>
    </Transition>

    <WorkspacePanel v-if="workbench" :workspace="workspace" :busy="busy || !ready" :connected="Boolean(link)" :current-instruction="activeLocal.instruction" @close="workbench = false" @assistant="startAssistant" @instruction="saveInstruction" @reuse="reuseSaved" @unsave="unsaveAnswer" @export-answer="exportAnswer" @export-chat="exportChat" @backup="exportBackup" @restore="restoreBackup" @document="chooseDocument" @save-prompt="savePrompt" @remove-prompt="removePrompt" @save-note="saveNote" @remove-note="removeNote" @attach-note="attachNote" @export-note="exportNote" @reading="saveReading" />

    <div v-if="renaming" class="scrim scrim--solid" @click="renaming = ''" />
    <form v-if="renaming" class="gate" role="dialog" aria-modal="true" aria-label="重命名会话" @submit.prevent="finishRename"><label class="field"><span>会话名称</span><input v-model="renameText" maxlength="80" aria-label="会话名称" /></label><div class="gate__row"><button class="ghost" type="button" @click="renaming = ''">取消</button><button class="gate__go" type="submit" :disabled="!renameText.trim()">保存名称</button></div></form>

    <!-- 两块整屏。通话垫在最前（它是"正在发生的事"），音色在它下面。 -->
    <VoicePanel v-if="link && stage === 'voice'" :link="link" @close="stage = ''" @changed="onVoiceChanged" />
    <CallStage v-if="link && stage === 'call'" :link="link" @close="stage = ''" @voice="onVoiceChanged" />

    <!-- 确认闸：她要在手机上做一件会留下痕迹的事，先在这里问一句。
         没有"超时自动同意"那种东西。 -->
    <div v-if="gate" class="scrim scrim--solid" @click="answerGate(false)" />
    <div v-if="gate" class="gate" role="dialog" aria-modal="true">
      <p class="gate__ask">{{ gate.prompt }}</p>
      <p class="gate__sub">这一步不做就什么都不会发生；做不做由你点。</p>
      <div class="gate__row">
        <button class="ghost" type="button" @click="answerGate(false)">取消</button>
        <button class="gate__go" type="button" @click="answerGate(true)">做</button>
      </div>
    </div>

    <section v-if="sheet === 'phone'" class="sheet">
      <div class="sheet__head">
        <h2>这台手机</h2>
        <button class="icon" type="button" aria-label="关闭" @click="sheet = ''">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          </svg>
        </button>
      </div>

      <div class="sheet__body">
        <div class="card">
          <h3>用大白话让她做</h3>
          <p class="hint">
            "把手电筒打开"、"七点叫我起床"、"给 10086 发个短信问余额"、"音量调到三成"——
            说在对话里就行，她自己去按。要出这台手机的（拨号、短信、日历、链接、闹钟）会先问你一句，最后那一下永远是你按的。
          </p>
          <p class="hint">
            <strong>她不做的事</strong>：读你的短信、读通讯录、代你点别的 App 的界面。
            那些要无障碍服务，装上等于把整台手机交出去。
          </p>
          <p v-if="link" class="hint hint--bad">
            现在连着电脑：这一轮问答走的是电脑上那个她，<strong>她够不到这台手机</strong>。
            要让手机动手，先断开电脑回到独立模式。
          </p>
        </div>

        <div v-if="quick.length" class="card">
          <h3>不用参数，可以直接按</h3>
          <div class="quick">
            <button
              v-for="action in quick"
              :key="action.name"
              type="button"
              :disabled="busy"
              @click="runManual(action)"
            >
              {{ action.label }}
            </button>
          </div>
        </div>

        <div class="card">
          <h3>相册</h3>
          <p class="hint">
            点开才会问你要相册权限。挑一张挂到输入框上，下一句她就真看得见那张图了——图不存进历史，历史里只留一个 📷。
          </p>
          <button class="ghost" type="button" :disabled="albumBusy" @click="openAlbum">
            {{ albumBusy ? '在读相册…' : album.length ? '重新读一次' : '读相册' }}
          </button>
          <p v-if="albumError" class="hint hint--bad">{{ albumError }}</p>
          <p v-else-if="albumEmpty" class="hint">{{ albumEmpty }}</p>
          <div v-if="album.length" class="grid">
            <button
              v-for="row in album"
              :key="row.uri"
              type="button"
              :title="row.name"
              @click="usePhoto(row)"
            >
              <img v-if="row.thumb" :src="row.thumb" :alt="row.name" loading="lazy" />
              <span v-else>没有缩略图</span>
            </button>
          </div>
        </div>

        <div class="card">
          <h3>她会做什么</h3>
          <ul class="acts">
            <li v-for="action in ACTIONS" :key="action.name">
              <span class="acts__name">{{ action.label }}</span>
              <span v-if="action.needsConfirm" class="acts__tag">要点头</span>
              <span v-if="action.leavesDevice" class="acts__tag acts__tag--out">出这台手机</span>
            </li>
          </ul>
        </div>

        <div class="card">
          <h3>最近她动过什么</h3>
          <ul v-if="ledger.length" class="log">
            <li v-for="(row, index) in ledger" :key="index" :class="{ 'log--bad': !row.ok }">
              <span class="log__at">{{ clockOf(row.at) }}</span>
              <span class="log__text">{{ row.detail }}</span>
            </li>
          </ul>
          <p v-else class="hint">还没在这台手机上动过手。第一笔会出现在这里，包括你取消的那些。</p>
          <button class="ghost" type="button" :disabled="!ledger.length" @click="clearLedger">
            清空这本账
          </button>
        </div>
      </div>
    </section>

    <section v-if="sheet === 'settings'" class="sheet">
      <div class="sheet__head">
        <h2>设置</h2>
        <button class="icon" type="button" aria-label="关闭" @click="sheet = ''">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          </svg>
        </button>
      </div>

      <div class="sheet__body">
        <div class="card">
          <h3>外观</h3>
          <div class="seg">
            <button type="button" :class="{ 'is-on': appearance === 'dark' }" @click="setAppearance('dark')">
              深色
            </button>
            <button type="button" :class="{ 'is-on': appearance === 'light' }" @click="setAppearance('light')">
              浅色
            </button>
          </div>
        </div>

        <div class="card">
          <h3>说话</h3>
          <label class="check">
            <input v-model="speakBack" type="checkbox" />
            <span>她的回答读出来</span>
          </label>
          <label class="check">
            <input v-model="handsFree" type="checkbox" />
            <span>免提连续对话</span>
          </label>
          <p class="hint">
            免提要真能听到才行：这台设备没有系统语音引擎时按钮会直接说不能用，不会转圈骗你。
          </p>
        </div>

        <div class="card">
          <h3>模型（独立模式，免 Key 端点可留空）</h3>
          <label class="field">
            <span>Base URL</span>
            <input v-model="form.baseUrl" type="text" spellcheck="false" />
          </label>
          <label class="field">
            <span>模型名</span>
            <input v-model="form.model" type="text" spellcheck="false" />
          </label>
          <label class="field">
            <span>API Key</span>
            <div class="secret">
              <input
                v-model="form.apiKey"
                class="secret__input"
                :class="{ 'is-masked': !showKey }"
                type="text"
                spellcheck="false"
                autocomplete="off"
                autocorrect="off"
                autocapitalize="off"
                :aria-label="showKey ? 'API Key（明文）' : 'API Key（已遮挡）'"
              />
              <button type="button" class="secret__eye" @click="showKey = !showKey">
                {{ showKey ? '隐藏' : '显示' }}
              </button>
            </div>
          </label>
          <p class="hint">
            Key 和电脑配对令牌由 Android Keystore 加密保存在本机；Key 只会发给你配置的模型端点。不要将自己的 Key、配对指纹和个人数据放入交付包。免 Key 端点可留空。网页预览只使用普通本机存储，请勿填真实密钥。连上电脑时用的是电脑上配好的那个模型，这里填的暂时用不上。
          </p>
          <label class="field">
            <span>证书指纹（自建 / 内网模型才要填）</span>
            <input v-model="form.pin" type="text" spellcheck="false" placeholder="留空 = 只认正规 CA" />
          </label>
          <p class="hint">
            留空时手机只认正规 CA 签发的地址，所以公司内网的网关、你自己机器上的
            Ollama / vLLM（自签证书）会连不上。填上那台服务器证书的 <code>sha256:…</code>
            就能连——这是<strong>用指纹替代 CA，不是把校验关掉</strong>：填错了就是连不上，
            不会退化成"随便谁都信"。公网服务（阿里、OpenRouter 这些）不要填。
          </p>
          <p class="hint">
            「拍一张」得换成能看图的模型（qwen-vl-plus 这一类）。用纯文本模型问图，屏幕上留下的是模型回的那句错，不会假装她看见了。
          </p>
          <p v-for="issue in modelIssues" :key="issue.field" class="hint hint--bad">{{ issue.text }}</p>
          <button class="wide" type="button" :disabled="probing" @click="tryModel">
            {{ probing ? '在试…' : '试一下这个配置（会真发一次最小请求）' }}
          </button>
          <p class="hint">
            探的是表单里这几栏、还没存的那份。404 是地址错了一层，401 是 Key 不对，
            422 多半是模型名——这三种在"连不上"三个字底下长得一模一样，分不开就只能一栏一栏试。
          </p>
          <button class="wide" type="button" @click="save">保存模型设置</button>
        </div>

        <div class="card">
          <h3>对话</h3>
          <button class="wide" type="button" @click="sheet = ''; workbench = true">AI 工作台、收藏与备份</button>
          <button class="wide wide--bad" type="button" :disabled="busy" @click="clearHistory">清空当前显示的对话</button>
          <p class="hint">清的是手机本机那一份。电脑上的历史要回电脑上删。</p>
        </div>

        <p class="foot">
          这版 App 是 {{ APP_VERSION }}。语音引擎是系统给的，不是这个 App 自带的。
        </p>
      </div>
    </section>

    <section v-if="sheet === 'connect'" class="sheet">
      <div class="sheet__head">
        <h2>连到电脑</h2>
        <button class="icon" type="button" aria-label="关闭" @click="sheet = ''">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          </svg>
        </button>
      </div>
      <div class="sheet__body">
        <p class="hint">
          电脑：托盘或顶栏打开「手机接入」→ 点「出一个配对码」，屏幕上会同时给出地址、端口和证书指纹。手机和电脑必须连同一个 Wi-Fi。
        </p>
        <label class="field">
          <span>电脑地址</span>
          <input v-model="pcForm.host" type="text" inputmode="decimal" placeholder="192.168.1.10" />
        </label>
        <label class="field">
          <span>端口</span>
          <input v-model="pcForm.port" type="number" inputmode="numeric" placeholder="8737" />
        </label>
        <label class="field">
          <span>证书指纹（照着电脑念）</span>
          <input v-model="pcForm.pin" type="text" spellcheck="false" placeholder="sha256:…" />
        </label>
        <label class="field">
          <span>配对码（6 位，5 分钟内有效）</span>
          <input v-model="pcForm.code" type="text" inputmode="numeric" maxlength="6" placeholder="000000" />
        </label>
        <div class="row">
          <button type="button" :disabled="busy" @click="test">先探一下</button>
          <button type="button" class="wide--go" :disabled="busy" @click="pair">配对</button>
          <button v-if="link" type="button" class="wide--bad" @click="unlink">断开</button>
        </div>
      </div>
    </section>

    <section v-if="sheet === 'pc'" class="sheet">
      <div class="sheet__head">
        <h2>这台电脑</h2>
        <button class="icon" type="button" aria-label="关闭" @click="sheet = ''">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
          </svg>
        </button>
      </div>
      <div class="sheet__body">
        <MobilePanels v-if="link" :link="link" />
        <p v-else class="hint">没连电脑，这一页没有内容。</p>
        <div class="row">
          <button type="button" @click="sheet = 'connect'">连电脑设置</button>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import brandLogo from '@/assets/brand/logo.webp'
import MoonPortrait from '@/avatar/MoonPortrait.vue'
/**
 * 手机版的主界面：一列、拇指够得着、说话键在右下角。
 *
 * 两种模式共用一套对话代码：`link` 在就走电脑上的 agent（能力面就是电脑那 18 个白名单方法），
 * 不在就用自己的 Key。区别只在 `answer()` 里那一次分支，界面上不许出现两套对话——
 * 两边说的话不一样，用户就分不清自己到底连没连上。
 *
 * 这一版相对上一版改了四件事，都是真机上按出来的结论：
 *  1. **键盘不再盖住输入框**：键盘高度由原生层量出来写进 `--xy-kb`，App 的高度
 *     从它算出来，底部输入区永远在键盘上面（为什么不能靠 `dvh`，见 MainActivity 顶部）。
 *  2. **底部只有一个输入胶囊**：上一版是「输入框 + 发送 + 拍照 + 说话」四个并排的方块，
 *     在窄屏上每个都不到 60px 宽，拇指按不准；现在附件收进「＋」，发送和麦克风占同一个位置。
 *  3. **会话从"一个抽屉"进**：上一版会话列表是一个从底部弹的面板，和"连电脑"抢同一个位置；
 *     现在历史走左侧抽屉，右侧那几个是设置和这台电脑。
 *  4. **每一条消息都能单独操作**：复制、朗读、重来、删除，就写在时间那一行。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import MessageBubble from './MessageBubble.vue'
import MobilePanels from './MobilePanels.vue'
import {
  APP_VERSION,
  appendHistory,
  appendLedger,
  loadHistory,
  loadLedger,
  loadModel,
  loadPc,
  loadVoiceFlags,
  pcCall,
  pcHello,
  pcPair,
  probeModel,
  saveModel,
  savePc,
  saveVoiceFlags,
  cancelChat,
  type Attachment,
  type ModelConfig,
  type PcLink,
  type Turn,
} from './api'
import { ACTIONS, execute, askMediaAccess, listAlbum, readAlbumPhoto, type AlbumPhoto, type PhoneAction } from './actions'
import { requestHistory } from './conversation'
import { askAgent } from './agent'
import { checkModel, checkPort, normalizePin, type FieldIssue } from './settings'
import { appearance, applyAppearance, setAppearance, toggleAppearance } from './appearance'
import CallStage from './CallStage.vue'
import { pickPhoto, takePhoto } from './photo'
import type { Delta } from './sse'
import type { ActionRow, PcMessageList, PcSessionList, PcSessionRow } from './types'
import { XySpeech, speakable } from './speech'
import { useReveal } from './typewriter'
import { dismissKeyboard, installViewport, keyboardOpen } from './viewport'
import VoicePanel from './VoicePanel.vue'
import WorkspacePanel from './WorkspacePanel.vue'
import { Preferences } from '@capacitor/preferences'
import { saveText } from './files'
import { PRESETS, MAX_SESSIONS, MAX_SAVED, MAX_BACKUP_BYTES, MAX_DOCUMENT_BYTES, MAX_TEXT, uid, createSession, forkSession, emptyWorkspace, parseWorkspace, backupWorkspace, searchSessions, sessionMarkdown, documentText, questionWithDocument, type Workspace, type SavedAnswer } from './workspace'
import { MAX_NOTES, MAX_PROMPTS, matchingMessages, type NotebookEntry, type PromptTemplate, type ReadingPreferences } from './productivity'

const HISTORY_CAP = 60
const WORKSPACE_KEY = 'xiaoye.workspace.v1'
const workspace = ref<Workspace>(emptyWorkspace())
const ready = ref(false)
const workbench = ref(false)
const sessionQuery = ref('')
const showArchive = ref(false)
const activeLocal = computed(() => workspace.value.sessions.find(s => s.id === workspace.value.active)!)
const localSessions = computed(() => searchSessions(workspace.value.sessions.filter(s => s.archived === showArchive.value), sessionQuery.value))
const documentInput = ref<HTMLInputElement | null>(null)
const pendingDocument = ref<{ name: string; text: string } | null>(null)
const renaming = ref(''), renameText = ref('')
let writes: Promise<boolean> = Promise.resolve(true)
function persistWorkspace(): Promise<boolean> {
  let value: string
  try { value = backupWorkspace(workspace.value) }
  catch (error) { note(`手机工作区没有保存：${String(error)}。请导出当前对话并清理较早内容`, true); return Promise.resolve(false) }
  writes = writes.then(async () => { await Preferences.set({ key: WORKSPACE_KEY, value }); return true }).catch(error => { note(`手机内容未保存，请导出备份：${String(error)}`, true); return false })
  return writes
}
async function persistHistory(): Promise<void> {
  if (link.value) { try { await appendHistory(turns.value) } catch (error) { note(`手机历史缓存没有保存：${String(error)}`, true) }; return }
  activeLocal.value.turns = turns.value.map(t => ({ ...t }))
  if (activeLocal.value.title === '新的对话') activeLocal.value.title = turns.value.find(t => t.role === 'user')?.text.slice(0, 36) || '新的对话'
  activeLocal.value.updated = Date.now()
  await persistWorkspace()
}
function presetName(id: string): string { return PRESETS.find(p => p.id === id)?.name || '日常助手' }
async function savePrompt(prompt: PromptTemplate): Promise<void> {
  if (busy.value || !ready.value) return
  if (!prompt.id && workspace.value.prompts.length >= MAX_PROMPTS) { note('自建提示词最多 50 条', true); return }
  const next = { ...prompt, id: prompt.id || uid() }
  const index = workspace.value.prompts.findIndex(p => p.id === next.id)
  if (index < 0) workspace.value.prompts.unshift(next); else workspace.value.prompts[index] = next
  if (await persistWorkspace()) note('提示词已保存')
}
async function removePrompt(id: string): Promise<void> {
  if (busy.value || !await approve('删除这条自建提示词？')) return
  workspace.value.prompts = workspace.value.prompts.filter(p => p.id !== id); await persistWorkspace()
}
async function saveNote(entry: NotebookEntry): Promise<void> {
  if (busy.value || !ready.value) return
  if (!entry.id && workspace.value.notes.length >= MAX_NOTES) { note('笔记和待办最多 100 条', true); return }
  const next = { ...entry, id: entry.id || uid(), updated: Date.now() }
  const index = workspace.value.notes.findIndex(n => n.id === next.id)
  if (index < 0) workspace.value.notes.unshift(next); else workspace.value.notes[index] = next
  if (await persistWorkspace()) note('笔记已保存')
}
async function removeNote(id: string): Promise<void> {
  if (busy.value || !await approve('删除这条笔记或待办？请先导出需要保留的内容。')) return
  workspace.value.notes = workspace.value.notes.filter(n => n.id !== id); await persistWorkspace()
}
function attachNote(entry: NotebookEntry): void {
  if (busy.value) return
  pendingDocument.value = { name: `${entry.title}.md`, text: entry.text || entry.title }
  workbench.value = false; note('笔记已作为资料添加，发送时才交给模型')
}
function exportNote(entry: NotebookEntry): void { void exportFile(`小夜-${entry.title.replace(/[<>:"/\\|?*\r\n]/g, '_')}.md`, `# ${entry.title}\n\n${entry.text}\n\n标签：${entry.tags}\n`, 'text/markdown') }
async function saveReading(reading: ReadingPreferences): Promise<void> { if (!busy.value && ready.value) { workspace.value.reading = reading; await persistWorkspace() } }
const chatSearch = ref(false), messageQuery = ref(''), matchCursor = ref(0)
const matches = computed(() => matchingMessages(turns.value, messageQuery.value))
async function jumpMatch(direction = 0): Promise<void> {
  if (!matches.value.length) return
  matchCursor.value = (matchCursor.value + direction + matches.value.length) % matches.value.length
  await nextTick()
  scroller.value?.querySelector(`[data-message-index="${matches.value[matchCursor.value]}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}
watch(messageQuery, () => { matchCursor.value = 0; void jumpMatch() })
async function archiveSession(id: string): Promise<void> { if (busy.value) return; const s = workspace.value.sessions.find(s => s.id === id); if (s) { s.archived = !s.archived; await persistWorkspace() } }
async function branchTurn(index: number): Promise<boolean> {
  if (busy.value || !ready.value) return false
  if (link.value) { note('分支保存在手机本地，请先断开电脑再使用', true); return false }
  if (workspace.value.sessions.length >= MAX_SESSIONS) { note('最多保留 100 段会话，请先备份整理', true); return false }
  const next = forkSession(activeLocal.value, index)
  workspace.value.sessions.push(next); await openLocalSession(next.id); note('已创建独立分支，原会话仍保留'); return true
}
function quoteTurn(index: number): void { const turn = turns.value[index]; if (turn && !busy.value) reuseSaved(`引用：\n${turn.text.slice(0, 12000).split('\n').map(line => `> ${line}`).join('\n')}\n\n我的问题：`) }
function transformTurn(index: number, kind: 'shorten' | 'checklist'): void { const turn = turns.value[index]; if (turn && !busy.value) reuseSaved(`${kind === 'shorten' ? '请用三句话概括以下内容，保留重要事实：' : '请把以下内容转为可执行的行动清单，不补充未知的负责人和时间：'}\n\n${turn.text.slice(0, 24000)}`) }
async function noteFromTurn(index: number): Promise<void> {
  const turn = turns.value[index]; if (!turn || busy.value) return
  if (turn.text.length > 24000) { note('回答超过笔记 24000 字限制，请先导出或分段保存', true); return }
  await saveNote({ id: '', title: turn.text.replace(/\s+/g, ' ').slice(0, 40) || '聊天笔记', text: turn.text, tags: '来自聊天', kind: 'note', done: false, pinned: false, updated: 0 })
}
async function openLocalSession(id: string): Promise<void> {
  if (busy.value || link.value || !ready.value) return
  const next = workspace.value.sessions.find(s => s.id === id)
  if (!next) return
  workspace.value.active = id
  turns.value = next.turns.map(t => ({ ...t }))
  draft.value = next.draft; pending.value = null; pendingDocument.value = null
  chatSearch.value = false; messageQuery.value = ''
  drawer.value = false
  await persistWorkspace(); await nextTick(); grow(); await scroll()
}
async function startAssistant(preset = 'general'): Promise<void> {
  if (busy.value || link.value || !ready.value) return
  if (workspace.value.sessions.length >= MAX_SESSIONS) { note('最多保留 100 段手机会话，请先备份并删除不需要的会话', true); return }
  const next = createSession(preset)
  workspace.value.sessions.push(next)
  await openLocalSession(next.id)
  workbench.value = false
  draft.value = PRESETS.find(p => p.id === preset)?.starter || ''
  await nextTick(); grow()
}
async function pinSession(id: string): Promise<void> {
  if (busy.value) return
  const s = workspace.value.sessions.find(s => s.id === id)
  if (s) { s.pinned = !s.pinned; await persistWorkspace() }
}
function renameSession(id: string): void {
  if (busy.value) return
  renaming.value = id
  renameText.value = workspace.value.sessions.find(s => s.id === id)?.title || ''
}
async function finishRename(): Promise<void> {
  const s = workspace.value.sessions.find(s => s.id === renaming.value)
  if (s && renameText.value.trim()) { s.title = renameText.value.trim().slice(0, 80); await persistWorkspace() }
  renaming.value = ''
}
async function deleteSession(id: string): Promise<void> {
  if (busy.value || !await approve('删除这段手机会话？这一步无法撤销，已收藏的回答会保留。')) return
  workspace.value.sessions = workspace.value.sessions.filter(s => s.id !== id)
  if (!workspace.value.sessions.length) workspace.value.sessions.push(createSession())
  if (workspace.value.active === id) { workspace.value.active = workspace.value.sessions[0]!.id; turns.value = activeLocal.value.turns.map(t => ({ ...t })); draft.value = activeLocal.value.draft; pending.value = null; pendingDocument.value = null }
  await persistWorkspace()
}
async function saveInstruction(text: string): Promise<void> {
  if (busy.value || link.value) return
  activeLocal.value.instruction = text.slice(0, 2000)
  if (await persistWorkspace()) note('当前会话指令已保存，下一条消息生效')
}
async function saveAnswer(index: number): Promise<void> {
  if (busy.value) return
  const turn = turns.value[index]
  if (!turn || !turn.text || workspace.value.saved.some(s => s.text === turn.text)) return
  if (workspace.value.saved.length >= MAX_SAVED) { note('最多收藏 200 条，请先导出或移除部分收藏', true); return }
  const question = turns.value.slice(0, index).reverse().find(t => t.role === 'user')?.text || ''
  workspace.value.saved.unshift({ id: uid(), text: turn.text.slice(0, MAX_TEXT), question: question.slice(0, MAX_TEXT), source: title.value.slice(0, 80), at: Date.now() })
  if (await persistWorkspace()) note('已加入工作台收藏')
}
async function unsaveAnswer(id: string): Promise<void> { workspace.value.saved = workspace.value.saved.filter(s => s.id !== id); await persistWorkspace() }
function reuseSaved(text: string): void { if (busy.value) return; draft.value = text; workbench.value = false; sheet.value = ''; void nextTick().then(grow) }
function editQuestion(index: number): void { const turn = turns.value[index]; if (!turn || busy.value) return; reuseSaved(turn.text.replace(/\s*📷$/, '')); note('已放入输入框，修改后会作为新问题发送') }
function followup(index: number): void { const turn = turns.value[index]; if (!turn || busy.value) return; reuseSaved(`针对这段回答，请进一步解释并举例：\n\n${turn.text.slice(0, 4000)}`) }
function chooseDocument(): void { if (busy.value) return; workbench.value = false; tray.value = false; sheet.value = ''; documentInput.value?.click() }
async function readDocument(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  input.value = ''
  if (!file || busy.value) return
  try {
    if (file.size > MAX_DOCUMENT_BYTES) throw new Error('资料最多 128 KB，请拆成小段')
    pendingDocument.value = documentText(file.name, file.size, await file.text())
    note('资料已添加，发送问题时才交给模型阅读')
  } catch (error) { note(error instanceof Error ? error.message : String(error), true) }
}
async function exportFile(name: string, text: string, mime: string): Promise<void> {
  try { note(await saveText(name, text, mime) ? '导出已完成' : '已取消保存') }
  catch (error) { note(error instanceof Error ? error.message : String(error), true) }
}
function exportChat(): void { if (!busy.value) void exportFile('小夜-对话.md', sessionMarkdown({ title: title.value, turns: turns.value }), 'text/markdown') }
function exportAnswer(answer: SavedAnswer): void { void exportFile('小夜-收藏.md', `# ${answer.source}\n\n${answer.question}\n\n${answer.text}\n`, 'text/markdown') }
function exportBackup(): void {
  if (busy.value) return
  try { void exportFile('小夜-手机备份.json', backupWorkspace(workspace.value), 'application/json') }
  catch (error) { note(error instanceof Error ? error.message : String(error), true) }
}
async function restoreBackup(file: File): Promise<void> {
  if (busy.value || link.value) return
  try {
    if (file.size > MAX_BACKUP_BYTES) throw new Error('备份不能超过 8 MB')
    const restored = parseWorkspace(await file.text())
    if (!await approve(`已检查备份：${restored.sessions.length} 段会话、${restored.saved.length} 条收藏、${restored.notes.length} 条笔记、${restored.prompts.length} 条提示词。恢复将替换当前手机内容，确定继续？`)) return
    if (busy.value || link.value) return
    await writes
    await Preferences.set({ key: WORKSPACE_KEY, value: JSON.stringify(restored) })
    workspace.value = restored
    ready.value = true
    turns.value = activeLocal.value.turns.map(t => ({ ...t }))
    pendingDocument.value = null; pending.value = null; draft.value = activeLocal.value.draft
    note('手机内容已恢复'); await scroll()
  } catch (error) { note(error instanceof Error ? error.message : String(error), true) }
}

const turns = ref<Turn[]>([])
const draft = ref('')
const busy = ref(false)
const listening = ref(false)
const toast = ref<{ text: string; bad: boolean } | null>(null)
const sheet = ref<'' | 'settings' | 'connect' | 'pc' | 'voice' | 'phone'>('')
/**
 * 通话页和音色页各自是一个整屏，而不是 sheet。
 *
 * 通话要盖住整页（说话时屏幕上只能有一个东西），音色页里要录一段几十秒的音，
 * 两者都不该和底部输入栏抢位置。`stage` 存的是"当前哪个整屏在前"。
 */
const stage = ref<'' | 'call' | 'voice'>('')
const drawer = ref(false)
const tray = ref(false)
const scroller = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
const far = ref(false)
const link = ref<PcLink | null>(null)
const cfg = ref<ModelConfig>({ baseUrl: '', apiKey: '', model: '', pin: '' })
const form = reactive<ModelConfig>({ baseUrl: '', apiKey: '', model: '', pin: '' })
const pcForm = reactive({ host: '', port: 8737, pin: '', code: '', device: '' })
const cap = reactive({ native: false, reason: '还没问', chinese: false })
const pending = ref<Attachment | null>(null)
const speakBack = ref(true)
const handsFree = ref(false)
/**
 * API Key 是不是明文显示。
 *
 * 这个框**故意不用 `type="password"`**：在 Android 上聚焦密码框会让输入法切到
 * 安全输入/独立编辑那一套，在 vivo 的 OriginOS 上直接把整个界面顶掉 ——
 * 屏幕上只剩 Activity 的窗口背景，看起来就是"整屏黑"，而进程还活着、没有崩溃日志。
 * 真机上量过：同一个面板里 `type="text"` 的字段顶栏有 3276 个亮像素，
 * `type="password"` 那个是 0。
 *
 * 遮挡改由 CSS 的 `-webkit-text-security: disc` 做，视觉一样，不碰系统的密码通路。
 */
const showKey = ref(false)
const sessions = ref<PcSessionRow[]>([])
const currentSession = ref('')

/**
 * 确认闸：模型要动这台手机时，屏幕上先出现这一问。
 *
 * 不设超时。**一个"30 秒不点就自动做"的确认框不是确认框**，是通知。
 * 人不在的时候它就一直等着，那一轮问答也就一直不结束——这比替人做主便宜。
 */
const gate = ref<{ prompt: string } | null>(null)
let resolveGate: ((go: boolean) => void) | null = null

function approve(prompt: string): Promise<boolean> {
  gate.value = { prompt }
  return new Promise<boolean>((done) => {
    resolveGate = done
  })
}

function answerGate(go: boolean): void {
  gate.value = null
  const done = resolveGate
  resolveGate = null
  done?.(go)
}

/** 她在手机上做过的事。存在这台手机上，见 `api.ts` 的 `loadLedger`。 */
const ledger = ref<ActionRow[]>([])
/**
 * 直接按的按钮：放**没有必填参数**的那几个，其余的靠说话让她做。
 *
 * 判据是"有没有必填参数"而不是"有没有参数"：`get_volume` 后来多了一个可选的
 * 声道，按老判据它会从这一排里消失——一个只加了可选项的动作不该因此少了个按钮。
 */
const quick = computed(() => ACTIONS.filter((action) => action.args.every((arg) => !arg.required)))

async function record(row: ActionRow): Promise<void> {
  ledger.value = [row, ...ledger.value].slice(0, 40)
  await appendLedger(ledger.value)
}

async function clearLedger(): Promise<void> {
  ledger.value = []
  await appendLedger([])
  note('这本账清空了。她做过的事不会因此没做过——只是屏幕上看不到了')
}

async function runManual(action: PhoneAction): Promise<void> {
  const result = await execute(action.name, {}, approve)
  await record({
    at: Date.now(),
    name: action.name,
    ok: result.ok,
    detail: result.text,
    outbound: action.leavesDevice,
  })
  note(result.text, !result.ok)
}

function clockOf(at: number): string {
  const stamp = new Date(at)
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${pad(stamp.getHours())}:${pad(stamp.getMinutes())}:${pad(stamp.getSeconds())}`
}

/** 相册浏览：缩略图列表。点开这一页才去要权限，不在启动时就问。 */
const album = ref<AlbumPhoto[]>([])
const albumError = ref('')
/** 读到了、但是一张都没有。跟 `albumError` 分开：这不是错误，是要给出下一步的空态。 */
const albumEmpty = ref('')
const albumBusy = ref(false)

async function openAlbum(): Promise<void> {
  if (albumBusy.value) return
  albumBusy.value = true
  albumError.value = ''
  albumEmpty.value = ''
  try {
    if (!(await askMediaAccess())) {
      albumError.value = '没给相册权限。她看不到你的照片；想问某一张，可以先用下面的"从相册"挑给她'
      album.value = []
      return
    }
    const seen = await listAlbum(24)
    album.value = seen.photos
    albumError.value = seen.error
    // 读到 0 张以前是**屏幕上一句话都没有**：面板空着，看着像按钮坏了。
    // 而 0 张有两种完全不同的原因（真没照片 / Android 14 只给"选中的那几张"却一张没选），
    // 两种都有下一步可走，所以要说出来。
    if (!seen.error && !seen.photos.length) {
      albumEmpty.value =
        '一张都没读到。可能是相册真的空着，也可能是 Android 14 那档"只给选中的照片"权限而你一张都没选——' +
        '换上面那个「从相册」按钮，让系统自己弹选择器，那条路不受这个限制'
    }
  } finally {
    albumBusy.value = false
  }
}

/** 挑一张挂到输入框上：图不进对话历史，只留一个 📷。 */
async function usePhoto(row: AlbumPhoto): Promise<void> {
  const shot = await readAlbumPhoto(row.uri, 1280)
  if (!shot) {
    note('这张读不出来（可能是云相册里还没下到本地的图）', true)
    return
  }
  pending.value = shot
  sheet.value = ''
  note('那张图挂在下一句上了，问她想问什么')
}

/** 正在逐字上屏的那一条的下标；-1 表示没有。 */
const streaming = ref(-1)
/**
 * 流式中的那一条气泡。
 *
 * 和 `reveal`（本地打字机）是两套上屏方式，**不能同时用**：模型已经在一段一段吐字了，
 * 再套一层逐字动画只会让界面比网络更慢，还会把"她其实卡住了"演成"她在慢慢说"。
 * 所以流式路径上 `reveal` 完全不参与。
 */
const live = ref(-1)
const liveText = ref('')

/** 距离上一次"有进展"过了几秒。进展 = 收到一段新字，或这一问刚开始。 */
const stall = ref(0)
/** 攒够这么多秒才在屏幕上开口，免得她还没想完就先闪一行字。 */
const STALL_SHOW_AFTER = 12
let stallTimer: ReturnType<typeof setInterval> | null = null

function startStall(): void {
  stopStall()
  stall.value = 0
  stallTimer = setInterval(() => {
    stall.value += 1
  }, 1000)
}

function stopStall(): void {
  if (stallTimer !== null) clearInterval(stallTimer)
  stallTimer = null
  stall.value = 0
}

/**
 * 断网。
 *
 * `navigator.onLine` 在 Android WebView 里跟的是系统那张网，**不保证真能连到模型那台机器**，
 * 所以它只用来提前说人话、省掉一次 20 秒的干等；真正算不算连不上，还是看请求回来的错误。
 */
const offline = ref(typeof navigator === 'undefined' ? false : navigator.onLine === false)

function onNetBack(): void {
  offline.value = false
  note('网通了')
}

function onNetGone(): void {
  offline.value = true
  note('这台手机断网了。她答不了话，输入框里的字会留着', true)
}

function beginLive(): void {
  turns.value = withTurn(turns.value, { role: 'assistant', text: '', at: Date.now() })
  live.value = turns.value.length - 1
  liveText.value = ''
  streaming.value = live.value
}

function onStreamDelta(delta: Delta): void {
  if (live.value < 0 || !delta.content) return
  stall.value = 0
  liveText.value += delta.content
  const at = live.value
  turns.value = turns.value.map((row, index) => (index === at ? { ...row, text: liveText.value } : row))
  void scroll()
}

/** 收尾：以最终文本为准（取消时就是已经吐出来的那半句）。 */
async function endLive(reply: string): Promise<void> {
  const at = live.value
  const fullText = reply || liveText.value
  const text = fullText.slice(0, MAX_TEXT)
  if (fullText.length > MAX_TEXT) note('回答超过 40000 字，本机保留前 40000 字，请拆成小问题继续', true)
  live.value = -1
  streaming.value = -1
  if (at >= 0 && at < turns.value.length) {
    turns.value = turns.value.map((row, index) => (index === at ? { ...row, text } : row))
  } else if (text) {
    turns.value = withTurn(turns.value, { role: 'assistant', text, at: Date.now() })
  }
  await persistHistory()
  await scroll()
}

/** 不听了。只有流式那一趟能停得动——遥控模式下话在电脑上跑，这边按了也拦不住。 */
async function stopAnswer(): Promise<void> {
  if (gate.value) answerGate(false)
  if (live.value < 0) return
  const cut = await cancelChat()
  note(cut ? '不再等她了，已经说出来的部分留着' : '这台设备停不下这一问')
}
const reveal = useReveal()
let teardown: (() => void) | null = null

const IDEAS_CHAT = ['用一句话解释量子纠缠', '帮我想三个周末去处', '这段话换个更客气的说法：']
const IDEAS_PC = ['这台电脑现在怎么样', '提醒我 10 分钟后喝水', '你还记得我什么']

const ideas = computed(() => (link.value ? IDEAS_PC : IDEAS_CHAT))

const title = computed(() => {
  if (!link.value) return activeLocal.value.title === '新的对话' ? presetName(activeLocal.value.preset) : activeLocal.value.title
  const row = sessions.value.find((item) => item.id === currentSession.value)
  return row?.title || '小夜'
})

const subtitle = computed(() => {
  // 断网要一直挂在顶上，不能只弹一条几秒就消失的提示：她答不上来的时候，
  // 人第一件要知道的事是"网没了"，不是"她坏了"。
  if (offline.value) return '这台手机现在没网'
  return link.value ? `遥控 · ${link.value.name}` : '独立模式 · 用自己的 Key'
})

function note(text: string, bad = false): void {
  toast.value = { text, bad }
  window.setTimeout(() => {
    // 只清掉自己那一条：后面的提示不该被前一条的定时器顺手抹掉。
    if (toast.value?.text === text) toast.value = null
  }, bad ? 5200 : 2400)
}

/** 逐字上屏时气泡要显示"已经放出来的那一段"，放完就回到完整原文。 */
function shown(turn: Turn, index: number): Turn {
  return index === streaming.value ? { ...turn, text: reveal.text.value } : turn
}

function hasAskBefore(index: number): boolean {
  for (let i = index - 1; i >= 0; i -= 1) {
    if (turns.value[i].role === 'user') return true
  }
  return false
}

async function scroll(): Promise<void> {
  await nextTick()
  const box = scroller.value
  if (box) box.scrollTop = box.scrollHeight
  far.value = false
}

function onScroll(): void {
  const box = scroller.value
  if (!box) return
  far.value = box.scrollHeight - box.scrollTop - box.clientHeight > 180
}

function grow(): void {
  const el = input.value
  if (!el) return
  // 先归零再量，否则删字之后高度只会涨不会落。
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 128)}px`
}

function onKey(event: KeyboardEvent): void {
  // 输入法组字中的回车是在选词，不是发送。
  if (event.isComposing) return
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void submit()
  }
}

/** 追加一轮并裁到上限。写成函数是为了让对象字面量拿到 `Turn` 这个上下文类型 ——
 * 直接 `turns.value = [...turns.value, { role: 'assistant', … }].slice(-60)` 时，
 * 上下文类型被 `.slice()` 截断，`role` 会被放宽成 `string`，类型检查直接红。 */
function withTurn(list: Turn[], turn: Turn): Turn[] {
  return [...list, turn].slice(-HISTORY_CAP)
}

async function push(role: Turn['role'], text: string, withPhoto = false, context?: string): Promise<void> {
  const body = withPhoto ? `${text} 📷` : text
  turns.value = withTurn(turns.value, { role, text: body, at: Date.now(), ...(context ? { context } : {}) })
  await persistHistory()
  await scroll()
}

async function answer(text: string, attachments: Attachment[]): Promise<string> {
  const bound = link.value
  if (bound) {
    const reply = await pcCall<{ answer?: string; error?: string }>(bound, 'chat_ask', {
      text,
      attachments,
    })
    if (reply.error) throw new Error(reply.error)
    return reply.answer ?? ''
  }
  // 独立模式走工具环：她在这台手机上能动的那些事，只有这一条路能真正落到原生层。
  // 遥控模式**不带**工具环——电脑上的那个 agent 够不到这台手机，把工具清单发过去
  // 只会让她答应做一件做不到的事（"我帮你把手机音量调大了"然后什么都没发生）。
  const instruction = [PRESETS.find(p => p.id === activeLocal.value.preset)?.instruction, activeLocal.value.instruction].filter(Boolean).join('\n\n')
  return askAgent(requestHistory(turns.value, text, workspace.value.reading.context), cfg.value, attachments, approve, {
    onAction: record,
    onNote: (message) => note(message),
    onDelta: live.value >= 0 ? onStreamDelta : undefined,
    toolsEnabled: workspace.value.reading.tools,
  }, instruction)
}

/** 一条她的回答上屏：先挂空壳，再逐字填，最后落历史。 */
async function show(reply: string): Promise<void> {
  if (reply.length > MAX_TEXT) { note('回答超过 40000 字，本机保留前 40000 字，请拆成小问题继续', true); reply = reply.slice(0, MAX_TEXT) }
  turns.value = withTurn(turns.value, { role: 'assistant', text: reply, at: Date.now() })
  streaming.value = turns.value.length - 1
  await scroll()
  await reveal.start(reply)
  streaming.value = -1
  await persistHistory()
  await scroll()
}

async function submit(): Promise<void> {
  const typed = draft.value.trim()
  const shot = pending.value
  const document = pendingDocument.value
  if ((!typed && !shot && !document) || busy.value || !ready.value) return
  const question = questionWithDocument(typed || (shot ? '这张图里是什么？说重点。' : ''), document)
  if (question.length > MAX_TEXT) { note('问题和资料合计不能超过 40000 字，请缩短后重试', true); return }
  // busy 必须**同步**置位。原来那句 `await nextTick()` 夹在"检查 busy"和"置 busy"之间，
  // 而只要让出一次执行权，快速双击（或者推荐问题＋回车）就能两次都通过检查——
  // 两条并发的问答会往同一个气泡里写字，看起来就是"她的回答串了"。
  busy.value = true
  if (offline.value) {
    // 断网就别发：发出去也是 20 秒后一句"连不上模型"。字留在输入框里，网通了再按。
    busy.value = false
    note('这台手机现在没网。你打的字还在输入框里，网通了再发一次', true)
    return
  }
  const attachments: Attachment[] = shot ? [shot] : []
  draft.value = ''
  pending.value = null
  pendingDocument.value = null
  tray.value = false
  await nextTick()
  grow()
  // 图不进历史：一张 300 KB 的 data URL 存 60 条就是 18 MB，而手机上根本不需要重放它。
  const display = document ? `${typed || '请总结这份资料的重点。'}\n\n📄 ${document.name}` : question
  await push('user', display, Boolean(attachments.length), document ? question : undefined)
  // 只有独立模式挂空气泡：遥控模式下答案在电脑上整段生成，这边挂个空壳
  // 只会得到一个"她在慢慢说"的假象——那是最骗人的一种等待。
  if (!link.value) beginLive()
  let reply = ''
  let problem = ''
  startStall()
  try {
    reply = await answer(question, attachments)
  } catch (err) {
    problem = err instanceof Error ? err.message : String(err)
  }
  stopStall()
  if (live.value >= 0) {
    // 出错也要收尾：半句已经上屏的话不能因为后面断了就整条消失，
    // 那比"她说到一半停住"更像坏了。
    await endLive(reply)
  } else if (reply) {
    await show(reply)
  }
  if (problem) note(problem, true)
  busy.value = false
  // 免提的"接着听"必须排在 busy 清零之后：startTalk 自己就拒 busy 状态的调用，
  // 排在 finally 前面等于按了不响——上一版就是这么哑掉的。
  if (!reply) return
  if (speakBack.value) await speak(reply)
  if (handsFree.value) await startTalk()
}

/** 空状态里的推荐问题：点一下直接问，不占输入框。 */
async function ask(text: string): Promise<void> {
  if (busy.value) return
  draft.value = text
  await submit()
}

async function shoot(which: 'camera' | 'photos'): Promise<void> {
  tray.value = false
  try {
    const shot = which === 'camera' ? await takePhoto() : await pickPhoto()
    if (shot) pending.value = shot
  } catch (err) {
    // 用户按"取消"也是一条 reject：那不该在屏幕上留下红色错误。
    const message = err instanceof Error ? err.message : String(err)
    if (!/cancel/i.test(message)) note(message || '取图没成功', true)
  }
}

async function speakTurn(index: number): Promise<void> {
  const turn = turns.value[index]
  if (turn) await speak(turn.text)
}

/**
 * 独立模式重问会创建分支，保留原会话；电脑模式只追加一次提问。
 *
 * 连电脑时这一问会**真的再问一次**，电脑那边也就真的多记一轮 —— 这是实话，
 * 手机上不假装"我帮你把电脑上那条撤回了"（`chat_delete` 不在白名单里，
 * 手机根本没有撤回的能力）。
 */
async function regenerate(index: number): Promise<void> {
  if (busy.value) return
  let askText = ''
  for (let i = index - 1; i >= 0; i -= 1) {
    if (turns.value[i].role === 'user') {
      askText = turns.value[i].context || turns.value[i].text.replace(/\s*📷$/, '')
      break
    }
  }
  if (!askText) return
  if (/📷\s*$/.test(turns.value.slice(0, index).reverse().find(t => t.role === 'user')?.text || '')) { note('图片不保留在历史中，请重新选图后发送，才能再次看图回答', true); return }
  reveal.cancel()
  streaming.value = -1
  if (!link.value) { if (!await branchTurn(index - 1)) return }
  else if (!await approve('将在电脑当前会话中重新提问，电脑原记录会保留。继续？')) return
  busy.value = true
  try {
    await persistHistory()
    if (!link.value) beginLive()
    const reply = await answer(askText, [])
    if (live.value >= 0) await endLive(reply)
    else await show(reply)
  } catch (err) {
    if (live.value >= 0) await endLive('')
    note(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function drop(index: number): Promise<void> {
  if (busy.value) return
  if (!await approve(link.value ? '仅从手机当前视图移除这条消息，电脑记录仍保留。继续？' : '删除这条手机消息？此操作无法撤销。')) return
  if (index === streaming.value) {
    reveal.cancel()
    streaming.value = -1
  }
  turns.value = turns.value.filter((_, i) => i !== index)
  await persistHistory()
  note('删掉了这一条')
}

async function loadSessions(): Promise<void> {
  const bound = link.value
  if (!bound) return
  try {
    const board = await pcCall<PcSessionList>(bound, 'chat_sessions')
    sessions.value = board.sessions ?? []
    currentSession.value = board.current ?? ''
    if (board.error) note(board.error, true)
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  }
}

async function openSession(id: string): Promise<void> {
  const bound = link.value
  if (!bound || busy.value) return
  busy.value = true
  drawer.value = false
  try {
    await pcCall<PcSessionList>(bound, 'chat_switch', { session_id: id })
    const seen = await pcCall<PcMessageList>(bound, 'chat_messages', { session_id: id })
    // 电脑上那一本按 role/content 存，手机气泡按 role/text 画；这里就是那道翻译。
    turns.value = (seen.messages ?? [])
      .filter((row) => row.role === 'user' || row.role === 'assistant')
      .slice(-HISTORY_CAP)
      .map(
        (row): Turn => ({
          role: row.role as Turn['role'],
          text: row.content,
          at: Number.isNaN(Date.parse(row.at)) ? undefined : Date.parse(row.at),
        }),
      )
    await appendHistory(turns.value)
    currentSession.value = id
    await scroll()
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function newSession(): Promise<void> {
  const bound = link.value
  if (busy.value || !ready.value) return
  if (!bound) { await startAssistant('general'); draft.value = ''; await nextTick(); grow(); return }
  busy.value = true
  try {
    await pcCall<PcSessionList>(bound, 'chat_new_session')
    turns.value = []
    await appendHistory([])
    await loadSessions()
    drawer.value = false
    note('已开一段新的（电脑那边那几段还在）')
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function speak(text: string): Promise<void> {
  const clean = speakable(text)
  if (!clean) return
  try {
    await XySpeech.speak({ text: clean.slice(0, 1200) })
  } catch {
    // 朗读失败不该把已经到屏的答案顶掉；这里刻意不做任何提示。
  }
}

async function startTalk(): Promise<void> {
  if (listening.value || busy.value) return
  // 要说话了，先把键盘收掉：一张还立着的键盘会盖住底部的"听着…"状态，
  // 用户按下去看到的是键盘，分不清麦克风到底开没开。
  dismissKeyboard()
  try {
    const started = await XySpeech.listenStart({ language: 'zh-CN', maxMs: 15000 })
    // 按下了但没开始录，一定要让用户看见：静默失败的麦克风比没有麦克风更难查。
    if (!started.ok) {
      note(started.error || '麦克风没打开', true)
      return
    }
    listening.value = true
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  }
}

async function stopTalk(): Promise<void> {
  if (!listening.value) return
  listening.value = false
  try {
    const heard = await XySpeech.listenStop()
    if (!heard.ok) {
      note(heard.error || '没听清', true)
      return
    }
    const text = heard.transcript.trim()
    if (!text) {
      note('没听到内容，再按一次说', true)
      return
    }
    draft.value = text
    await nextTick()
    grow()
    await submit()
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  }
}

/** 免提中途关掉、或者手指滑出按钮：把识别器关掉，但别把这半句话当成一次提问发出去。 */
async function cancelTalk(): Promise<void> {
  if (!listening.value) return
  listening.value = false
  try {
    await XySpeech.listenStop()
    // 取消这件事**必须在屏幕上留一句话**。原来这里是静默的：手指滑出按钮，
    // 那半句就没了，用户看到的是"她没听见我说什么"——而其实是自己取消了。
    note('这一段没发出去（滑出按钮就是取消），要发就再按住说一次')
  } catch {
    // 停下来这一步本身失败没有别的可见后果，不该在屏幕上再叠一条红色错误。
  }
}

/** 模型那几栏挑出来的毛病。空表就是可以存。 */
const modelIssues = ref<FieldIssue[]>([])
const probing = ref(false)

async function save(): Promise<void> {
  // 校验不过就**不存**：存进去一个连不上的地址，屏幕上却写着"已保存"，
  // 下一次问答失败时人根本想不起来是这一栏的问题。
  modelIssues.value = checkModel(form)
  if (modelIssues.value.length) {
    note(`先改一下：${modelIssues.value[0].text}`, true)
    return
  }
  // 指纹归一化之后写回表单：从终端里复制出来的那串常常没有 `sha256:` 前缀，
  // 补上是这里的活，不该让用户猜格式。
  const pin = normalizePin(form.pin) ?? ''
  const next = { baseUrl: form.baseUrl.trim(), apiKey: form.apiKey.trim(), model: form.model.trim(), pin }
  try {
    await saveModel(next)
    cfg.value = next
    form.pin = pin
    note('模型设置已安全保存')
  } catch (error) {
    note(error instanceof Error ? error.message : '安全配置保存失败，仍使用原设置', true)
  }
}

/**
 * 「试一下这个配置」：探的是**表单里这份还没存的**，不是已经存下的那份。
 * 存之前先知道通不通，就不用改一栏存一次、再问一句才发现不对。
 */
async function tryModel(): Promise<void> {
  modelIssues.value = checkModel(form)
  if (modelIssues.value.length) {
    note(`先改一下：${modelIssues.value[0].text}`, true)
    return
  }
  probing.value = true
  try {
    const seen = await probeModel({
      baseUrl: form.baseUrl.trim(),
      apiKey: form.apiKey.trim(),
      model: form.model.trim(),
      pin: normalizePin(form.pin) ?? '',
    })
    note(seen.text, !seen.ok)
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  } finally {
    probing.value = false
  }
}

async function clearHistory(): Promise<void> {
  if (busy.value || !await approve('清空当前显示的对话？手机独立会话会清空，电脑原始历史不会被删除。')) return
  reveal.cancel()
  streaming.value = -1
  turns.value = []
  await persistHistory()
  note('这台手机上的对话已清空')
}

async function test(): Promise<void> {
  const bad = checkPort(pcForm.port, 8737)
  if (bad) {
    note(bad.text, true)
    return
  }
  busy.value = true
  try {
    const hello = await pcHello(pcForm.host, Number(pcForm.port), pcForm.pin)
    note(
      `找到了 ${hello.name} ${hello.version}${
        hello.pairing_open ? '，配对码窗口是开的' : '，电脑上还没出配对码'
      }`,
    )
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function pair(): Promise<void> {
  const bad = checkPort(pcForm.port, 8737)
  if (bad) {
    note(bad.text, true)
    return
  }
  busy.value = true
  try {
    const bound = await pcPair({
      host: pcForm.host,
      port: Number(pcForm.port),
      pin: pcForm.pin,
      code: pcForm.code,
      device: pcForm.device || '手机',
    })
    link.value = bound
    turns.value = []
    pending.value = null; pendingDocument.value = null; draft.value = ''
    await savePc(bound)
    sheet.value = ''
    note(`连上了 ${bound.name}`)
    await loadSessions()
    if (currentSession.value) {
      busy.value = false
      await openSession(currentSession.value)
    }
  } catch (err) {
    note(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function unlink(): Promise<void> {
  if (busy.value) return
  link.value = null
  sessions.value = []
  currentSession.value = ''
  turns.value = activeLocal.value.turns.map(t => ({ ...t }))
  draft.value = activeLocal.value.draft; pending.value = null; pendingDocument.value = null
  await savePc(null)
  sheet.value = ''
  note('已断开这台电脑，回到独立模式')
}

watch([handsFree, speakBack], async ([free, back]) => {
  await saveVoiceFlags({ speakBack: back, handsFree: free })
  // 勾掉免提就得立刻松手：一个停不下来的麦克风在手机上不是"少个功能"，是赖着不走。
  if (!free && listening.value) await cancelTalk()
})

watch(drawer, (open) => {
  if (open && link.value) void loadSessions()
})
let draftTimer: ReturnType<typeof setTimeout> | undefined
watch(draft, (text) => {
  if (!ready.value || link.value) return
  activeLocal.value.draft = text.slice(0, MAX_TEXT)
  clearTimeout(draftTimer)
  draftTimer = setTimeout(() => { void persistWorkspace() }, 350)
}, { flush: 'sync' })
function flushDraft(): void { clearTimeout(draftTimer); if (ready.value) void persistWorkspace() }

/**
 * 打开通话页。
 *
 * 三个前置条件都在这里说清，而不是让用户对着一个按不动的话筒猜：
 * 没连电脑（手机上没有她）、没连上麦克风（浏览器预览）、以及没选中任何音色 ——
 * 最后一条不算错，但**要说出来**，否则用户会以为她默认会用某种声音说话。
 */
function openCall(): void {
  if (!link.value) {
    note('通话要让电脑上的她听和说，先连上电脑。', true)
    return
  }
  stage.value = 'call'
}

/**
 * 打开音色面板。
 *
 * 未连电脑时**也显示这个按钮**，只是点下去给一句实话。原来它带 `v-if="link"`，
 * 于是独立模式下整个入口消失 —— 装上包的人看不到通话和音色，只能猜是不是没做。
 * 「不存在的功能」和「有但需要先连电脑的功能」是两件事，界面上必须分得开：
 * 藏起来说不清，明说一句才说得清。
 *
 * 音色库整体在电脑上（`voice_clone_list` / `tts_voices` 都是桥面方法），
 * 所以这里没有"独立模式下也能用一部分"的中间态可做 —— 只能老实说清楚。
 */
function openVoice(): void {
  tray.value = false
  if (!link.value) {
    note('音色要用电脑上的声音库，先连上电脑。', true)
    return
  }
  stage.value = 'voice'
}

/** 音色变了之后弹一条 —— 用户回到主界面时要知道这次改动生效了。 */
function onVoiceChanged(): void {
  note('音色换好了，她下一句就用这个声音。')
}

onMounted(async () => {
  teardown = installViewport()
  window.addEventListener('online', onNetBack)
  window.addEventListener('offline', onNetGone)
  window.addEventListener('pagehide', flushDraft)
  applyAppearance()
  const startupWarnings: string[] = []
  try { cfg.value = await loadModel() }
  catch (error) { startupWarnings.push(error instanceof Error ? error.message : '模型安全配置读取失败') }
  Object.assign(form, cfg.value)
  try { link.value = await loadPc() }
  catch (error) { startupWarnings.push(error instanceof Error ? error.message : '电脑配对配置读取失败') }
  try {
    const stored = (await Preferences.get({ key: WORKSPACE_KEY })).value
    workspace.value = stored ? parseWorkspace(stored) : emptyWorkspace(link.value ? [] : await loadHistory())
    if (!stored) await Preferences.set({ key: WORKSPACE_KEY, value: backupWorkspace(workspace.value) })
  } catch (error) {
    startupWarnings.push(`手机工作区未能读取，原记录仍保留：${String(error)}`)
    for (const warning of startupWarnings) note(warning, true)
    return
  }
  if (link.value) {
    Object.assign(pcForm, {
      host: link.value.host,
      port: link.value.port,
      pin: link.value.pin,
    })
    await loadSessions()
    if (currentSession.value) await openSession(currentSession.value)
  } else {
    turns.value = activeLocal.value.turns.map(t => ({ ...t }))
    draft.value = activeLocal.value.draft
  }
  ready.value = true
  for (const warning of startupWarnings) note(warning, true)
  ledger.value = await loadLedger()
  const wanted = await loadVoiceFlags()
  handsFree.value = wanted.handsFree
  speakBack.value = wanted.speakBack
  const seen = await XySpeech.capability()
  Object.assign(cap, seen)
  await scroll()
})

onBeforeUnmount(() => {
  flushDraft()
  if (gate.value) answerGate(false)
  void cancelChat()
  teardown?.()
  reveal.cancel()
  stopStall()
  window.removeEventListener('online', onNetBack)
  window.removeEventListener('offline', onNetGone)
  window.removeEventListener('pagehide', flushDraft)
})
</script>

<style scoped>
.session-filters { display:flex; gap:8px; margin:0 16px 8px; } .session-filters button { min-height:40px; padding:8px 12px; color:var(--xy-text-2); font-size:12px; border:1px solid var(--xy-line); border-radius:10px; } .session-filters button[aria-pressed=true] { color:var(--xy-accent); background:var(--xy-accent-soft); }
.chat-search { display:flex; gap:6px; align-items:center; padding:8px 12px; border-bottom:1px solid var(--xy-line); flex-shrink:0; } .chat-search input { min-width:0; flex:1; padding:10px; border:1px solid var(--xy-line-2); border-radius:10px; background:var(--xy-card); color:var(--xy-text); font:inherit; } .chat-search button { min-width:36px; min-height:44px; color:var(--xy-accent); } .chat-search span { font-size:12px; color:var(--xy-text-2); white-space:nowrap; } .search-match { outline:2px solid var(--xy-accent); border-radius:12px; outline-offset:2px; }
.app--large :deep(.bubble) { font-size:19px; line-height:1.85; } .app--compact :deep(.acts) { display:none; } .app--compact :deep(.turn:focus-within .acts),.app--compact :deep(.turn--expanded .acts) { display:flex; }
.hidden-file { display: none; }
.document-attach { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; padding: 8px 16px; font-size: 12px; color: var(--xy-text-2); background: var(--xy-card); }
.document-attach span { flex: 1; overflow-wrap: anywhere; }
.document-attach button { min-height: 44px; color: var(--xy-accent); }
.local-search { margin: 8px 16px; padding: 12px; min-height: 44px; border: 1px solid var(--xy-line-2); border-radius: 12px; background: var(--xy-card); color: var(--xy-text); width: calc(100% - 32px); box-sizing: border-box; }
.local-session__tools { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 8px; }
.local-session__tools button { min-height: 36px; padding: 6px 8px; font-size: 11px; color: var(--xy-text-2); }
.app {
  isolation: isolate;
  /* 钉在视口上，用 `bottom` 把键盘那块让出来。
   *
   * 为什么是 fixed 而不是 `height: calc(100% - ...)`：在设置面板里点一个靠底部的
   * 输入框时，Chromium 会为了露出光标去滚**文档**，而文档一旦被滚走，
   * 正常流里的界面就整块出了屏幕 —— 屏幕上只剩 Activity 的窗口背景
   * （那张 Capacitor 启动图），看起来就是"整屏黑"，但进程还活着、没有崩溃日志。
   * `html/body` 上的 `overflow: hidden/clip` 挡不住它。
   *
   * fixed 定位的元素不受文档滚动影响，这是唯一一处"钉住"就够用的地方。
   * 高度仍然由 `--xy-kb`（原生层报的键盘高度）决定，见 MainActivity 顶部。 */
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: var(--xy-kb, 0px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--xy-bg);
  color: var(--xy-text);
  padding-top: env(safe-area-inset-top);
}

/* -- 顶栏 ---------------------------------------------------------------- */

.top {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 8px 8px 6px;
}

.top__mid {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  line-height: 1.25;
}

.top__name {
  font-size: 15px;
  font-weight: 600;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.top__sub {
  font-size: 11px;
  color: var(--xy-text-3);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.top__sub--live {
  color: var(--xy-accent);
}

.icon {
  flex: 0 0 auto;
  width: var(--xy-tap);
  height: var(--xy-tap);
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--xy-pill);
  background: transparent;
  color: var(--xy-text-2);
}

.icon:active {
  background: var(--xy-card-2);
  color: var(--xy-text);
}

.icon:disabled {
  opacity: 0.3;
}

/* 「有这个功能，但要先连电脑」。
 *
 * 和 `:disabled` 的 0.3 刻意不同：那个意思是"现在按不动"，这个意思是
 * "按了会告诉你为什么"。所以留住可辨识的对比度（0.45），并且**仍然可按** ——
 * 把按钮做成灰的又什么都不说，正是这一版要修掉的那个毛病。 */
.icon.is-waiting,
.tray button.is-waiting {
  opacity: 0.45;
}

/* -- 横幅 ---------------------------------------------------------------- */

.banner {
  flex: 0 0 auto;
  margin: 0 12px 4px;
  padding: 9px 12px;
  border-radius: var(--xy-r-sm);
  background: var(--xy-card);
  font-size: 12px;
  line-height: 1.6;
  color: var(--xy-text-2);
  /* 它现在是个 button（可点去配对），所以要自己把浏览器给的默认外观清掉：
     背景、边框、字体、对齐。少清一样就会在真机上露出一圈系统色。 */
  display: block;
  width: calc(100% - 24px);
  border: 0;
  text-align: left;
  font-family: inherit;
}

.banner strong {
  color: var(--xy-text);
  font-weight: 600;
}

.banner em {
  font-style: normal;
  color: var(--xy-accent);
}

.banner:active {
  background: var(--xy-card-2);
}

.banner--bad {
  background: var(--xy-danger-soft);
  color: var(--xy-danger);
}

/* 键盘一弹，屏幕就少了一半。这条横幅是"还没连电脑"的说明，
   正在打字的人不需要再看一遍 —— 让位给消息流。 */
.app--kb .banner {
  display: none;
}

/* -- 消息流 -------------------------------------------------------------- */

.stream {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  padding: 8px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.hello {
  margin: auto 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 24px 8px;
  text-align: center;
}

.hello__orb {
  width: 54px;
  height: 54px;
  border-radius: var(--xy-pill);
  background: var(--xy-accent-soft);
  border: 1px solid var(--xy-accent-line);
  position: relative;
}

.hello__orb::after {
  content: '';
  position: absolute;
  inset: 18px;
  border-radius: var(--xy-pill);
  background: var(--xy-accent);
}

.hello__title {
  margin: 4px 0 0;
  font-size: 19px;
  font-weight: 600;
}

.hello__sub {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--xy-text-2);
  max-width: 300px;
}

.hello__ideas {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 8px;
}

.hello__ideas button {
  padding: 9px 14px;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line);
  background: var(--xy-card);
  color: var(--xy-text-2);
  font-size: 13px;
}

.hello__ideas button:active {
  border-color: var(--xy-accent-line);
  color: var(--xy-text);
}

.thinking {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0;
  padding-left: 36px;
  font-size: 12px;
  color: var(--xy-text-3);
}

/* 卡住那一行：同一档字号，只换个颜色——要看得见，但不能比"她在想"更抢眼。 */
.thinking--slow {
  display: block;
  line-height: 1.5;
  color: var(--xy-warn);
}

.thinking__dot {
  width: 5px;
  height: 5px;
  border-radius: var(--xy-pill);
  background: var(--xy-accent);
  animation: xy-bounce 1.1s ease-in-out infinite;
}

.thinking__dot:nth-child(2) {
  animation-delay: 0.15s;
}

.thinking__dot:nth-child(3) {
  animation-delay: 0.3s;
  margin-right: 6px;
}

@keyframes xy-bounce {
  0%,
  60%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

.jump {
  position: absolute;
  right: 16px;
  /* 落在输入区上方，不挡住发送键。App 的高度就是可视区高度，
     所以这里不需要再为键盘补一段偏移。 */
  bottom: calc(96px + env(safe-area-inset-bottom));
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line);
  background: var(--xy-card);
  color: var(--xy-text-2);
  box-shadow: var(--xy-shadow-soft);
}

/* -- 待发送的图 ---------------------------------------------------------- */

.attach {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-top: 1px solid var(--xy-line);
  background: var(--xy-bg-soft);
  font-size: 12px;
  color: var(--xy-text-2);
}

.attach img {
  height: 52px;
  width: 52px;
  border-radius: var(--xy-r-sm);
  object-fit: cover;
  border: 1px solid var(--xy-line);
}

.attach span {
  flex: 1;
}

.attach button {
  border: 0;
  border-radius: var(--xy-pill);
  padding: 7px 12px;
  background: var(--xy-danger-soft);
  color: var(--xy-danger);
  font-size: 12px;
}

/* -- 输入区 -------------------------------------------------------------- */

.dock {
  flex: 0 0 auto;
  border-top: 1px solid var(--xy-line);
  background: var(--xy-bg-soft);
  padding: 8px 10px calc(8px + env(safe-area-inset-bottom));
}

.tray {
  flex-wrap: wrap;
  display: flex;
  gap: 8px;
  padding: 2px 0 10px;
  overflow-x: auto;
}

.tray button {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  min-width: 72px;
  padding: 12px 10px;
  border-radius: var(--xy-r-md);
  border: 1px solid var(--xy-line);
  background: var(--xy-card);
  color: var(--xy-text-2);
  font-size: 12px;
}

.tray button:active {
  border-color: var(--xy-accent-line);
  color: var(--xy-text);
}

.dock__bar {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}

.dock__plus,
.dock__mic,
.dock__send {
  flex: 0 0 auto;
  width: var(--xy-tap);
  height: var(--xy-tap);
  display: grid;
  place-items: center;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line);
  background: var(--xy-card);
  color: var(--xy-text-2);
}

.dock__plus.is-on {
  transform: rotate(45deg);
  border-color: var(--xy-accent-line);
  color: var(--xy-accent);
}

.dock__send {
  border-color: transparent;
  background: var(--xy-accent);
  color: var(--xy-on-accent);
}

.dock__stop {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 5px;
  height: var(--xy-tap);
  padding: 0 14px;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-danger-soft);
  background: var(--xy-danger-soft);
  color: var(--xy-danger);
  font-size: 13px;
}

.dock__send:disabled,
.dock__mic:disabled {
  opacity: 0.4;
}

.dock__mic.is-on {
  border-color: var(--xy-accent);
  color: var(--xy-accent);
  background: var(--xy-accent-soft);
}

.dock__input {
  flex: 1;
  min-width: 0;
  /* 自适应高度由 grow() 写 inline height，这里给上下限兜底。 */
  min-height: var(--xy-tap);
  max-height: 128px;
  padding: 11px 14px;
  border-radius: var(--xy-r-lg);
  border: 1px solid var(--xy-line);
  background: var(--xy-card);
  color: var(--xy-text);
  line-height: 1.5;
  resize: none;
  overflow-y: auto;
}

.dock__input::placeholder {
  color: var(--xy-text-3);
}

.dock__why {
  margin: 8px 4px 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--xy-text-3);
}

/* -- 提示条 -------------------------------------------------------------- */

.toast {
  position: absolute;
  left: 16px;
  right: 16px;
  bottom: calc(84px + env(safe-area-inset-bottom));
  margin: 0;
  padding: 11px 14px;
  border-radius: var(--xy-r-md);
  background: var(--xy-card-2);
  border: 1px solid var(--xy-line);
  box-shadow: var(--xy-shadow);
  font-size: 13px;
  line-height: 1.5;
  color: var(--xy-text);
  z-index: 30;
}

.toast--bad {
  border-color: var(--xy-danger);
  color: var(--xy-danger);
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

/* -- 抽屉与面板 ---------------------------------------------------------- */

.scrim {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 9;
}

.drawer {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: min(80%, 320px);
  z-index: 10;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: calc(14px + env(safe-area-inset-top)) 12px calc(14px + env(safe-area-inset-bottom));
  background: var(--xy-bg-soft);
  border-right: 1px solid var(--xy-line);
  transform: translateX(-102%);
  transition: transform 0.22s ease;
}

.drawer--on {
  transform: translateX(0);
}

.drawer__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.drawer__brand {
  font-size: 15px;
  font-weight: 600;
  padding-left: 8px;
}

.drawer__new {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 12px;
  border-radius: var(--xy-r-md);
  border: 1px solid var(--xy-accent-line);
  background: var(--xy-accent-soft);
  color: var(--xy-accent);
  font-size: 14px;
}

.drawer__new:disabled {
  opacity: 0.4;
}

.drawer__list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.drawer__list button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  width: 100%;
  padding: 11px 12px;
  border-radius: var(--xy-r-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--xy-text-2);
  font-size: 13px;
  text-align: left;
}

.drawer__list button.is-on {
  background: var(--xy-accent-soft);
  border-color: var(--xy-accent-line);
  color: var(--xy-text);
}

.drawer__title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.drawer__meta {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--xy-text-3);
}

.drawer__empty {
  margin: 0;
  padding: 16px 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--xy-text-3);
}

.drawer__foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--xy-line);
}

.drawer__ver {
  font-size: 11px;
  color: var(--xy-text-3);
}

.ghost {
  padding: 8px 12px;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line);
  background: transparent;
  color: var(--xy-text-2);
  font-size: 12px;
}

.sheet {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 12;
  display: flex;
  flex-direction: column;
  max-height: 88%;
  border-radius: var(--xy-r-xl) var(--xy-r-xl) 0 0;
  background: var(--xy-bg-soft);
  border-top: 1px solid var(--xy-line);
  box-shadow: var(--xy-shadow);
}

.sheet__head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 8px 6px 16px;
}

.sheet__head h2 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}

.sheet__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  padding: 4px 16px calc(20px + env(safe-area-inset-bottom));
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-radius: var(--xy-r-lg);
  background: var(--xy-card);
  border: 1px solid var(--xy-line);
}

.card h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--xy-text-2);
}

.seg {
  display: flex;
  gap: 6px;
  padding: 3px;
  border-radius: var(--xy-pill);
  background: var(--xy-card-2);
}

.seg button {
  flex: 1;
  padding: 9px;
  border: 0;
  border-radius: var(--xy-pill);
  background: transparent;
  color: var(--xy-text-2);
  font-size: 13px;
}

.seg button.is-on {
  background: var(--xy-accent);
  color: var(--xy-on-accent);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field span {
  font-size: 12px;
  color: var(--xy-text-3);
}

.field input {
  padding: 12px 14px;
  border-radius: var(--xy-r-sm);
  border: 1px solid var(--xy-line);
  background: var(--xy-bg-soft);
  color: var(--xy-text);
}

/* API Key 那一格：输入框 + 「显示/隐藏」。
   遮挡是 CSS 做的，不是 `type="password"` —— 后者在 Android 上会把输入法切到
   安全输入那一套，在部分 ROM 上整个界面被顶掉（详见 script 里 showKey 的注释）。 */
.secret {
  display: flex;
  align-items: stretch;
  gap: 8px;
}

.secret__input {
  flex: 1;
  min-width: 0;
}

.secret__input.is-masked {
  -webkit-text-security: disc;
}

.secret__eye {
  flex: 0 0 auto;
  padding: 0 14px;
  border-radius: var(--xy-r-sm);
  border: 1px solid var(--xy-line);
  background: var(--xy-card-2);
  color: var(--xy-text-2);
  font-size: 13px;
}

.check {
  display: flex;
  align-items: center;
  gap: 10px;
}

.check input {
  width: 20px;
  height: 20px;
  accent-color: var(--xy-accent);
}

.check span {
  font-size: 14px;
}

.hint {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--xy-text-3);
}

.row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.row button,
.wide {
  padding: 12px 16px;
  border-radius: var(--xy-r-md);
  border: 1px solid var(--xy-line);
  background: var(--xy-card);
  color: var(--xy-text);
  font-size: 14px;
}

.wide {
  width: 100%;
}

.row .wide--go,
.wide--go {
  border-color: transparent;
  background: var(--xy-accent);
  color: var(--xy-on-accent);
}

.wide--bad,
.row .wide--bad {
  border-color: var(--xy-danger-soft);
  background: var(--xy-danger-soft);
  color: var(--xy-danger);
}

.foot {
  margin: 4px 0 0;
  font-size: 11px;
  color: var(--xy-text-3);
}

/* -- 确认闸与"这台手机"面板 ------------------------------------------------ */

.scrim--solid {
  z-index: 20;
  background: rgba(0, 0, 0, 0.62);
}

.gate {
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  z-index: 21;
  width: min(88%, 420px);
  padding: 18px;
  border-radius: var(--xy-r-lg);
  border: 1px solid var(--xy-accent-line);
  background: var(--xy-bg-soft);
  box-shadow: var(--xy-shadow);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.gate__ask {
  margin: 0;
  font-size: 15px;
  line-height: 1.6;
  color: var(--xy-text);
  word-break: break-word;
}

.gate__sub {
  margin: 0;
  font-size: 11px;
  line-height: 1.7;
  color: var(--xy-text-3);
}

.gate__row {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}

.gate__go {
  min-width: 96px;
  padding: 10px 16px;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-accent-line);
  background: var(--xy-accent-soft);
  color: var(--xy-accent);
  font-size: 14px;
}

.quick {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(84px, 1fr));
  gap: 6px;
  margin-top: 8px;
}

.grid button {
  aspect-ratio: 1;
  padding: 0;
  border-radius: var(--xy-r-md);
  border: 1px solid var(--xy-line);
  background: var(--xy-card);
  color: var(--xy-text-3);
  font-size: 10px;
  overflow: hidden;
}

.grid img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.quick button {
  padding: 9px 14px;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line-2);
  background: var(--xy-card-2);
  color: var(--xy-text);
  font-size: 13px;
}

.quick button:disabled {
  opacity: 0.45;
}

.acts {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.acts li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--xy-text-2);
}

.acts__name {
  min-width: 72px;
  color: var(--xy-text);
}

.acts__tag {
  padding: 1px 7px;
  border-radius: var(--xy-pill);
  border: 1px solid var(--xy-line);
  font-size: 10px;
  color: var(--xy-warn);
}

.acts__tag--out {
  border-color: var(--xy-danger-soft);
  color: var(--xy-danger);
}

.log {
  margin: 0 0 8px;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 34dvh;
  overflow-y: auto;
}

.log li {
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--xy-text-2);
}

.log__at {
  font-family: var(--xy-mono);
  font-size: 11px;
  opacity: 0.8;
}

.log__text {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}

.log--bad .log__text {
  color: var(--xy-danger);
}
</style>
