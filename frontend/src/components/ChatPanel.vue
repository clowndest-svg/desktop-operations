<template>
  <section class="hud-panel chat" :class="{ 'chat--max': expanded }">
    <!--
      The rail only exists in the expanded view. Inline, the session list is a popup
      you have to open; enlarged, it is the left column of the same screen, because
      "which conversation am I in" is a question worth seeing while you read one.
    -->
    <aside v-if="expanded" class="chat__rail">
      <div class="chat__rail-head">
        <span class="hud-label">会话 · {{ sessions.length }}</span>
        <button class="hud-btn" type="button" :disabled="working" @click="createSession">＋新</button>
      </div>
      <ul class="chat__rail-list">
        <li v-for="session in sessions" :key="session.id">
          <button
            class="chat__session"
            :class="{ 'chat__session--on': session.id === activeSession }"
            type="button"
            :title="`${session.title} · ${session.turns} 轮`"
            @click="openSession(session.id)"
          >
            <span class="chat__session-title">{{ session.title }}</span>
            <span class="chat__session-meta">{{ session.turns }} 轮 · {{ stamp(session.updated_at) }}</span>
          </button>
        </li>
      </ul>
      <p v-if="railError" class="chat__rail-error">{{ railError }}</p>
    </aside>

    <div class="chat__main">
      <div class="chat__toolbar">
        <span class="chat__heading">对话</span>
        <button class="chat__model-label" type="button" title="选择模型与对话选项" :aria-expanded="optionsOpen" @click="optionsOpen = !optionsOpen">{{ models.find(entry => entry.id === model)?.label || '选择模型' }} <span aria-hidden="true">⌄</span></button>
        <button class="chat__simple" type="button" :disabled="working" @click="addTab">新对话</button>
        <button class="chat__simple" type="button" @click="sessionsOpen = true">历史</button>
        <button class="chat__simple" type="button" aria-label="对话选项" :aria-expanded="optionsOpen" aria-controls="chat-options" @click="optionsOpen = !optionsOpen">···</button>
      </div>
      <div v-show="optionsOpen" id="chat-options" class="chat__options" aria-label="对话选项">
      <header class="hud-title">
        对话选项
        <button class="hud-btn chat__clear" type="button" :disabled="busy || lines.length === 0" @click="clear">
          清空
        </button>
        <button class="hud-btn chat__export" type="button" :disabled="busy || lines.length === 0" @click="exportChat">
          导出
        </button>
        <!-- 用量放在对话区而不是设置里：它是"这场对话花了多少"，不是"机器配置"。 -->
        <button class="hud-btn chat__usage" type="button" @click="emit('usage')">用量</button>
        <button
          class="hud-btn chat__voice"
          type="button"
          title="换她说话的声音（试听不用先选上）"
          @click="emit('voice')"
        >
          音色
        </button>
        <!--
          放大 is the answer to "对话框太小了": the same conversation, the same state,
          filling the window. It re-positions this one element rather than opening a
          second copy, because two components reading one transcript is how the two
          views start disagreeing about what was said.
        -->
        <button
          v-if="!expanded"
          class="hud-btn chat__expand"
          type="button"
          title="放大到整窗，在弹窗里对话"
          @click="toggleExpanded"
        >
          放大
        </button>
        <!--
          The model picker lives here rather than only in the settings dialog because
          that is where the answer appears: you decide "this reply was too dumb / too
          slow / too expensive" while looking at the reply. It writes the same
          preference the settings screen writes -- one source of truth, so the two can
          never disagree about which model is answering.

          Two dropdowns, not one, because a key belongs to a provider and a provider
          offers several models. The first picks the vendor, the second picks which of
          its models answers; collapsing them into one list would repeat the vendor on
          every row and lose the "switch vendor, keep the model when it exists there"
          gesture.

          A model whose key is missing is still listed, marked: hiding it would make
          "why is there no gpt option" the question, and the answer is a one-line
          environment variable the operator can set themselves.
        -->
        <select
          v-if="providers.length > 1"
          class="hud-field chat__model"
          :value="provider"
          :disabled="switching"
          title="服务商"
          @change="chooseProvider"
        >
          <option v-for="entry in providers" :key="entry.name" :value="entry.name">
            {{ entry.name }}{{ entry.key_set ? '' : needsKey(entry) ? '（缺 key）' : '（免 Key）' }}
          </option>
        </select>
        <select
          v-if="models.length > 1 || modelRowLocked"
          class="hud-field chat__model"
          :value="model"
          :disabled="switching"
          :title="
            modelRowLocked
              ? '自带模型：这一格只报当前在跑的那一档，想换去「设置 → 本地模型」。名字里带「仅冒烟」的档位只够验证能不能连通，别拿它聊天'
              : '该服务商下的大模型'
          "
          @change="chooseModel"
        >
          <option v-for="entry in models" :key="entry.id" :value="entry.id">{{ entry.label }}</option>
        </select>
        <!--
          Thinking budget and context size are per model, so switching a model carries
          its own pair along. Both are one line next to the conversation on purpose: a
          knob you have to open a dialog to reach is a knob nobody turns mid-question.
        -->
        <select
          v-if="thinkingLevels.length"
          class="hud-field chat__knob"
          :value="thinking"
          :disabled="switching"
          :title="thinkingTitle"
          @change="saveThinking"
        >
          <option v-for="level in thinkingLevels" :key="level" :value="level">{{ levelLabel(level) }}</option>
        </select>
        <select
          v-if="turnsBounds.length === 2"
          class="hud-field chat__knob"
          :value="String(turns)"
          :disabled="switching"
          title="最多携带的历史轮数；本地模型会按Token预算裁剪旧上下文，完整历史仍保留"
          @change="saveTurns"
        >
          <option v-for="count in turnOptions" :key="count" :value="String(count)">{{ count }} 轮上下文</option>
        </select>
        <!--
          工具表是她那双手的清单，这一格决定每次请求带不带它。不是偏好而是端点的上限：
          实测 34 个 schema 要 4639 token，一个 4096 上下文的本地模型连「你好」都装不下，
          所以这一格只能按"这一对模型"存，而不是全局一个开关。
        -->
        <label class="chat__knob chat__knob--check" title="每次请求带不带工具表（不带就没有那双手）">
          <input type="checkbox" :checked="tools" :disabled="switching" @change="saveTools" />
          带工具
        </label>
        <!--
          协作形态。三种形态只差一件事：谁能看见谁说的话。
          价格写在这一行里，因为点下去之后才知道"这一句问了九次"是没有回头路的。
        -->
        <select
          class="hud-field chat__knob"
          :class="{ 'chat__knob--on': !single }"
          :value="mode"
          :title="modeTitle"
          @change="setMode"
        >
          <option v-for="key in MODE_ORDER" :key="key" :value="key">{{ MODE_LABELS[key] }}</option>
        </select>
      </header>
      </div>

      <!--
        放大态的关闭放在右上角 —— 和其余十三扇弹窗同一个位置、同一个词。
        以前它叫「收回」，蹲在顶栏那一排按钮中间，找它得把顶栏扫一遍；而窗口右上角
        空着，那里恰恰是所有人本能会去看的地方。绝对定位而不是塞进 header：
        .hud-title 有个 ::after{flex:1} 在吃剩余空间画那条渐变线，往 header 里加
        margin-left:auto 会把线挤没。
      -->
      <button
        v-if="expanded"
        class="hud-btn chat__close"
        type="button"
        title="关闭对话大窗（Esc）"
        @click="toggleExpanded"
      >
        关闭
      </button>

      <!--
        椅子。顺序就是点它的顺序，而"顺序意味着什么"跟着形态变：圆桌里第一家负责合并，
        主管模式里第一家只拆和收、不动手，投票里顺序无所谓。所以标题跟着形态换字。
      -->
      <div v-if="!single" class="chat__seats">
        <button
          v-for="option in seatOptions"
          :key="option.key"
          class="hud-btn chat__seat"
          :class="{ 'chat__seat--on': picked(option.key) }"
          type="button"
          :title="option.vision"
          @click="pickSeat(option.key)"
        >
          {{ option.label }}<small v-if="option.sight"> · {{ option.sight }}</small>
        </button>
        <select
          v-if="mode === 'table'"
          class="hud-field chat__seat-rounds"
          :value="String(rounds)"
          title="讨论几轮：每轮每家说一次，说完再合并"
          @change="setRounds"
        >
          <option v-for="count in [1, 2, 3]" :key="count" :value="String(count)">{{ count }} 轮</option>
        </select>
        <span v-else-if="mode === 'boss'" class="hud-label chat__seat-hint">
          第一家是主管（只拆和收，不干活），{{ Math.max(0, seats.length - 1) }} 家干活
        </span>
        <span v-else-if="mode === 'vote'" class="hud-label chat__seat-hint">
          各答各的，然后匿名互评
        </span>
        <span class="chat__seat-cost hud-num">≈ {{ requests }} 次请求</span>
      </div>

      <!--
        One row per conversation, each with its own status. The strip reads the pushed
        snapshot rather than keeping score locally, because two tabs can be answering at
        the same moment and "which one is still working" has exactly one right answer.

        It is drawn even when there is only one card: the ＋ that opens a second
        conversation lives in here, so the old `v-if="tabs.length > 1"` (whose second
        half said exactly the same thing) was a row that would swallow the door if the
        list ever came up with a single card. Packaged builds start with two cards, so
        this was a latent trap rather than something a customer hit.
      -->
      <nav v-show="tabs.length > 1" class="chat__tabs" aria-label="对话列表">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          class="hud-btn chat__tab"
          :class="{ 'chat__tab--active': tab.active, 'chat__tab--busy': tab.status === 'running' }"
          type="button"
          :title="tabTitle(tab)"
          @click="openTab(tab.id)"
        >
          <i class="chat__tab-dot" aria-hidden="true"></i>
          <span class="chat__tab-name">{{ tab.title || '新对话' }}</span>
          <small v-if="tab.status === 'running'" class="chat__tab-phase">{{ tab.phase }}</small>
          <small v-else-if="tab.status === 'failed'" class="chat__tab-phase">没答上</small>
        </button>
        <button class="hud-btn chat__tab chat__tab--new" type="button" title="另开一个对话" @click="addTab">
          ＋
        </button>
      </nav>

      <div ref="listRef" class="chat__log" @scroll.passive="onLogScroll">
        <p v-if="lines.length === 0" class="hud-label chat__empty">
          想聊什么，直接告诉我。也可以点下方的语音按钮。
        </p>
        <div v-for="(line, index) in lines" :key="index" class="chat__turn" :class="`chat__turn--${line.role}`">
          <div class="chat__bubble" @contextmenu.prevent="textMenu($event, index, line.text)">
            <span class="chat__who">{{ line.role === 'user' ? '你' : '小夜' }}</span>
            <div v-if="line.attachments.length" class="chat__files">
              <template v-for="(file, slot) in line.attachments" :key="slot">
                <img v-if="file.kind === 'image' && file.data" class="chat__thumb" :src="file.data" :alt="file.name" />
                <span v-else class="chat__file">{{ file.name }} · {{ formatBytes(file.size) }}</span>
              </template>
            </div>
            <span class="chat__text" @contextmenu.stop.prevent="textMenu($event, index, line.text)"><template v-for="(part, slot) in textParts(line.text)" :key="slot"><a v-if="part.url" :href="part.url" target="_blank" rel="noopener noreferrer">{{ part.text }}</a><template v-else>{{ part.text }}</template></template></span>
            <!--
              这一轮产出的文件：图就地画出来，其余给一行名字加三颗按钮。

              桌面版没有「下载」这个动作 —— 文件本来就在盘上，所以人要的是"打开它"和
              "让我看见它在哪儿"，而不是一个假的进度条。门答 `ok: false` 时那句原因落在
              这一行的按钮下面，不许只进 console：按了没反应和"没权限/文件不在"在屏上
              必须是两件事。
            -->
            <div v-if="line.role === 'assistant' && line.artifacts.length" class="chat__arts">
              <div
                v-for="(art, slot) in line.artifacts"
                :key="`art-${index}-${slot}`"
                class="chat__art"
                @contextmenu.stop.prevent="artMenu($event, art)"
              >
                <img
                  v-if="isPicture(art) && pictureOf(art)"
                  class="chat__art-pic"
                  :src="pictureOf(art)"
                  :alt="baseName(art)"
                  @dblclick="openArt(art, 'open')"
                />
                <p v-else-if="isPicture(art)" class="chat__art-wait">{{ waitWords(art) }}</p>
                <!--
                  文件不是"一行名字加三颗按钮"，是一张卡片：左边一枚按扩展名上色的角标，
                  中间名字 + 大小两行，右边只留一颗主操作和一颗 ⋯。四颗按钮平铺会把
                  名字挤到换行，用户说的"太丑"就是这一处。
                -->
                <video v-else-if="suffixOf(art) === '.mp4' && videos[art]" :src="videos[art]" controls loop playsinline class="chat__art-pic"></video>
                <div v-else class="chat__art-file">
                  <span class="chat__art-icon" :data-ext="extLabel(art)">{{ extLabel(art) }}</span>
                  <span class="chat__art-meta">
                    <span class="chat__art-name">{{ baseName(art) }}</span>
                    <span class="chat__art-sub">{{ fileSub(art) }}</span>
                  </span>
                  <span class="chat__art-acts">
                    <button class="hud-btn chat__art-btn" type="button" @click="openArt(art, 'open')">
                      打开
                    </button>
                    <button
                      class="chat__art-more"
                      type="button"
                      aria-label="更多操作"
                      @click="artMenu($event, art)"
                    >⋯</button>
                  </span>
                </div>
                <span v-if="isPicture(art)" class="chat__art-row">
                  <button class="hud-btn chat__art-btn" type="button" @click="copyPicture(art)">复制图片</button>
                  <button class="hud-btn chat__art-btn" type="button" @click="openArt(art, 'open')">打开</button>
                  <button class="hud-btn chat__art-btn" type="button" @click="openArt(art, 'folder')">所在文件夹</button>
                </span>
                <p v-if="artWhy(art)" class="chat__art-why">{{ artWhy(art) }}</p>
              </div>
            </div>
            <!--
              气泡这段话本身也要能抄走 —— 但它是这一条的**页脚**，不是第四颗按钮：
              和"本轮调用了哪条工具"并成一行，右击那段话也能拿到同一件事。

              先试浏览器那条路（`api/clipboard.ts` 会照实回报 copied / empty / failed
              三种结果），它说没成才走 Windows 剪贴板那扇门 —— 两扇都说没成，屏上就写着
              没成，按钮不许假装成功。
            -->
            <div
              v-if="line.role === 'assistant'"
              class="chat__footline"
              @contextmenu.stop.prevent="textMenu($event, index, line.text)"
            >
              <button class="chat__foot-btn" type="button" @click="copyBubble(index, line.text)">
                {{ copyMark(index) }}
              </button>
              <span v-if="line.tools" class="chat__tools">{{ toolReport(line.tools) }}</span>
              <span v-if="copyWhy(index)" class="chat__art-why">{{ copyWhy(index) }}</span>
            </div>
            <details v-if="line.role === 'assistant' && line.reasoning" class="chat__reasoning">
              <summary class="hud-label">思考过程</summary>
              <pre class="chat__reasoning-text">{{ line.reasoning }}</pre>
            </details>
            <!--
              Who said what around the table. Its own fold rather than folded into the
              answer: the conclusion is what gets read aloud and replayed, and three
              opinions in the bubble would bury the one thing that was asked for.
            -->
            <details v-if="line.role === 'assistant' && line.record" class="chat__reasoning">
              <summary class="hud-label">圆桌记录</summary>
              <pre class="chat__reasoning-text">{{ line.record }}</pre>
            </details>
            <!--
              Whether this answer actually had hands. The 带工具 box says what the setting
              asks for; only this says what went out, which is the difference between
              "she won't" and "she was never given the tool".
            -->
            <div v-if="line.role === 'assistant' && line.tools" class="chat__tools">
              {{ toolReport(line.tools) }}
            </div>
          </div>
        </div>
        <!--
          The answer while it is still arriving. A separate bubble rather than an edit of
          ``history``: the replay list is what the next request is built from, and a
          half-finished sentence in there would be fed back to the model as something she
          already decided.
        -->
        <div v-if="streamed && streamed.text" class="chat__turn chat__turn--assistant">
          <div class="chat__bubble chat__bubble--streaming">
            <span class="chat__who">{{ streamed.model || '小夜' }}</span>
            <span class="chat__text">{{ streamed.text }}</span>
            <span class="chat__caret" aria-hidden="true"></span>
          </div>
        </div>
        <!--
          A question with no visible answer is indistinguishable from a hung app, and a
          model turn can take twenty seconds. The bubble appears the moment the request
          leaves, so the silence in between has a shape.

          Two doors say "she is working": the task table (a typed question, which knows
          the phase) and the shell's turn state (a spoken one, which never enters the
          table). Reading only the first is why 语音提问 never got this bubble.
        -->
        <div
          v-if="(runningTask || voice.turn === 'processing') && !streamed?.text"
          class="chat__turn chat__turn--assistant"
        >
          <div class="chat__bubble chat__bubble--thinking">
            <span class="chat__who">小夜</span>
            <!--
              Dots alone read as "some app is loading". The words name what is happening,
              and the phase underneath names which of the several waiting it is: a tool call
              and a round-table seat speaking are not the same silence.
            -->
            <span
              class="chat__loader"
              :class="`chat__loader--${loaderKind}`"
              role="status"
              aria-label="思考中"
              ><i v-for="cell in 6" :key="cell"></i
            ></span>
            <span class="chat__thinking-word">思考中</span>
            <span v-if="thinkingPhase" class="chat__thinking-phase">{{ thinkingPhase }}</span>
            <!--
              On the bubble rather than only in the toolbar, because this is the thing
              beside it you would point at: 停止 ends the round she is in, 回复 puts the
              cursor in the box so the next question can be written while she works --
              and it will wait its turn rather than talk over this one.
            -->
            <span class="chat__thinking-actions">
              <button
                v-if="runningTask"
                class="hud-btn hud-btn--warn"
                type="button"
                title="掐掉这一轮"
                @click="stop"
              >
                停止
              </button>
              <button
                class="hud-btn"
                type="button"
                title="接着提下一个问题（这一句会排在后面）"
                @click="askBack"
              >
                回复
              </button>
            </span>
          </div>
        </div>
        <!--
          She asked a model something and got nothing back. Until this card existed the
          reason stayed in logs/jarvis.log and the screen showed only the operator's own
          bubble, which is indistinguishable from a hang -- and the two have opposite fixes.
          The words go where the answer would have gone. The advice is looked up by the
          error's own kind, never by reading its sentence: rewording a message must not
          silently turn a helpful card back into a bare stack of letters.
        -->
        <!--
          这一轮答上了，不过是把工具表扔了才装进它的上下文的。不当失败画（失败卡一多，
          人就学会忽略失败卡），也不能不说：「她能帮我开软件」在这一轮并不成立。
        -->
        <p v-if="turnNote" class="chat__note">{{ turnNote }}</p>
        <div v-if="failure" class="chat__turn chat__turn--assistant">
          <div class="chat__bubble chat__bubble--failed">
            <span class="chat__who">小夜</span>
            <span class="chat__failed-title">这句我没答上</span>
            <p class="chat__failed-reason">{{ failure.reason }}</p>
            <p v-if="failure.hint" class="chat__failed-hint">{{ failure.hint }}</p>
            <p v-if="failure.where" class="chat__failed-where">{{ failure.where }}</p>
            <details v-if="failure.detail" class="chat__reasoning">
              <summary class="hud-label">完整报错（可以整段抄给服务商）</summary>
              <pre class="chat__reasoning-text">{{ failure.detail }}</pre>
            </details>
          </div>
        </div>
        <div v-if="error" class="chat__error">{{ error }}</div>
      </div>

      <!--
        Attachments are shrunk in the page before they go anywhere: a 4 MB photo
        becomes a ~30 KB thumbnail, which is what a chat bubble and a model context
        window both actually want. What cannot be shrunk (video, documents) travels
        as a name and a size, and the panel says so instead of implying the assistant
        watched the film.
      -->
      <div v-if="pending.length" class="chat__pending">
        <span v-for="(file, slot) in pending" :key="slot" class="chat__chip">
          <img v-if="file.kind === 'image' && file.data" :src="file.data" alt="" />
          {{ file.name }}
          <button class="chat__chip-x" type="button" title="去掉这个附件" @click="pending.splice(slot, 1)">×</button>
        </span>
      </div>

      <!--
        The questions she has not been asked yet, in the order they were typed. They are
        also in the transcript above -- this strip is not a second copy of the record, it
        is the only place each one can be taken back out before she reaches it.
      -->
      <div v-if="queued.length" class="chat__queue">
        <span class="chat__queue-label">排队中 {{ queued.length }}</span>
        <span v-for="(item, slot) in queued" :key="item.task_id" class="chat__chip chat__chip--queued">
          <span class="chat__queue-seq">{{ slot + 1 }}</span>
          {{ item.question }}
          <button class="chat__chip-x" type="button" title="撤回这一句，不问了" @click="withdraw(item.task_id)">×</button>
        </span>
      </div>

      <p v-if="editing" class="hud-label">正在修改历史提问：发送后新建对话分支，原记录保留。
        <button class="hud-btn" type="button" @click="editing = null">取消修改</button>
      </p>
      <form class="chat__form" @submit.prevent="send">
        <button class="hud-btn chat__attachment" type="button" aria-label="添加附件" title="添加图片、视频或文件" @click="openFiles">＋</button>
        <input ref="fileRef" type="file" multiple hidden @change="onFiles" />
        <!--
          Not disabled while she thinks. That was the single-conversation assumption
          leaking into the input: with several conversations open, the box being shut
          because *another* tab is waiting is just a locked keyboard. And with this tab
          waiting, what comes out of it is a queued question, not a second answer.
        -->
        <input
          ref="inputRef"
          v-model="draft"
          class="hud-field chat__input"
          type="text"
            placeholder="和小夜聊聊…"
          maxlength="2000"
          @paste="onPaste"
        />
          <button class="hud-btn chat__talk" type="button" :disabled="voice.loading || busy" :title="talkTitle" @click="talk">{{ voice.loading ? '准备中' : listening ? '聆听中…' : '语音' }}</button>
        <button v-if="runningTask" class="hud-btn hud-btn--warn" type="button" title="掐掉这一轮" @click="stop">
          停止
        </button>
        <button class="hud-btn" type="submit" :disabled="draft.trim() === ''">
          发送
        </button>
      </form>

      <p v-if="shownNotice" class="chat__notice">{{ shownNotice }}</p>

        <!--
          Only in the expanded view: what is answering, and how long the conversation
          is. Inline there is no room for it, and enlarged the panel can finally say the
          two things a long conversation makes worth reading without opening 用量.
        -->
        <footer v-if="expanded" class="chat__foot">
          <!--
            Per-conversation, not global: three tabs on three models is the whole point
            of the tab strip, and the backend has stored this since the multi-conversation
            round -- only the door was missing.
          -->
          <select
            v-if="conversationModelChoices.length > 1"
            class="hud-field chat__knob"
            :value="activeTabModelKey"
            :disabled="switching"
            title="这一会话用哪家哪个模型；别的会话不受影响"
            @change="pinConversationModel"
          >
            <option v-for="pair in conversationModelChoices" :key="pair.key" :value="pair.key">
              {{ pair.label }}
            </option>
          </select>
          <span v-else class="hud-label">当前模型 {{ currentLabel || '—' }}</span>
          <span v-if="pinNote" class="hud-label">{{ pinNote }}</span>
          <span class="hud-label">
            {{ THINKING_LABELS[thinking] ?? thinking }} · {{ turns }} 轮上下文 ·
            {{ tools ? '带工具' : '不带工具' }}
          </span>
          <span class="hud-label">{{ lines.length }} 条 · 会话 {{ activeTitle }}</span>
          <span class="hud-label">Esc 关闭</span>
        </footer>
      </div>

    <SessionsPopup :open="sessionsOpen" @close="sessionsOpen = false" @switched="loadCurrent" />
  </section>
</template>

<script lang="ts">
/**
 * 本轮画出来的那一张图，按路径缓存 —— 声明在**模块级**，不是 setup 里的一个 ref。
 *
 * 这一屏每 1.5 秒就被快照重画一次，而 `artifact_read` 是真去读盘、真去缩图的一扇门。
 * 在渲染函数里直接问它，就是每 1.5 秒把每一张图重读一遍 —— 那正是这个窗口为"轮询把活
 * 干了两遍"付过账的形状。所以：一条路径问一次（``pictureAsked``），答案留到下次挂载还在
 * （``pictureCache``），失败的也留着 —— 它要在屏上站着，不许退回空白。
 */
const pictureCache = new Map<string, string>()
const pictureAsked = new Set<string>()
</script>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { describeToolReport as toolReport } from '../chat/toolReport'
import {
  artifactRead,
  artifactVideo,
  artifactStat,
  chatCancel,
  chatSend,
  chatEditQuestion,
  chatCollaborate,
  copyImage,
  copyTextViaBridge,
  exportConversation,
  fetchModels,
  fetchSessionMessages,
  fetchSessions,
  formatBytes,
  newConversation,
  newSession,
  openArtifact,
  openConversation,
  pickConversationModel,
  pickModel,
  saveTuning,
  switchSession,
  type ActionResult,
  type ArtifactHow,
  type Attachment,
  type ConversationCard,
  type ModelSpec,
  type ProviderChoice,
  type QueuedQuestion,
  type SessionInfo,
  type ToolReport,
} from '@/api/bridge'
import { copyText, type CopyOutcome } from '@/api/clipboard'
import { useContextMenu, type MenuEntry } from '@/composables/useContextMenu'
import { readAsDataUrl, shrinkToBudget } from '@/api/thumbnail'
import { stamp } from '@/api/time'
import {
  MODE_LABELS,
  needsKey,
  requestsFor,
  type CollaborationMode,
  type ModelCap,
  type SeatChoice,
} from '@/api/bridge'
import { useVoiceStore } from '@/stores/voice'
import SessionsPopup from '@/components/SessionsPopup.vue'

/** 下拉框里的顺序，和后端 `COLLABORATION_MODES` 是同一批值。 */
const MODE_ORDER: CollaborationMode[] = ['single', 'table', 'boss', 'vote']

/** 一桌几家、最多几轮。后端 jarvis/app/round_table.py 里各有一份，测试钉住不许漂。 */
const MAX_SEATS = 4
const MAX_ROUNDS = 3

// The dialog itself lives in App.vue: this panel only asks for it.
const emit = defineEmits<{ (e: 'usage'): void; (e: 'voice'): void }>()

const voice = useVoiceStore()

/** The four shapes the shell can name. Kept in step with `preferences.THINKING_LOADERS`. */
const LOADER_KINDS = ['dots', 'matrix', 'ring', 'bars']

const draft = ref('')
const editing = ref<{ conversation: string; id: number } | null>(null)
const busy = ref(false)
const localError = ref('')
const listRef = ref<HTMLElement | null>(null)

const sessionsOpen = ref(false)

/**
 * What is on screen: the stored turns of the open conversation, then the live
 * turns that arrived since it was loaded.
 *
 * Two sources, one list, no duplicates: the store is read once per session switch
 * and the live feed is sliced from where the seed ended. Reading the store on
 * every poll instead would race the very turns it is supposed to be showing.
 */
interface Line {
  id?: number
  role: string
  text: string
  attachments: Attachment[]
  /**
   * The model's own reasoning, when the setting asked for it.
   *
   * Only the live turn has it: the transcript table stores questions and answers, not
   * thinking, so a reloaded session renders no chain rather than pretending to.
   */
  reasoning?: string
  /**
   * Everything said around a round table, in order.
   *
   * Live turns only, like ``reasoning``: the transcript stores the question and the
   * merged answer, so a reloaded session shows the conclusion without the argument.
   */
  record?: string
  /**
   * Which model answered, shown as that turn's name.
   *
   * Live turns only, like ``reasoning`` -- the transcript table has a column for the
   * model but the reload path does not fill this, so carrying it means "the one still
   * on screen", not "the one that was stored".
   */
  model?: string
  /**
   * What this answer's request carried: tool count, pack, what was called, why fewer.
   *
   * Live turns only. The transcript stores questions and answers, so a reloaded row
   * has nothing to say about a request it never saw -- and drawing 「本次 0 个工具」
   * under an old answer would be inventing that.
   */
  tools?: ToolReport
  /**
   * Files this turn put on disk, in the shape the shell sends them.
   *
   * Live turns carry it straight from the snapshot. A reloaded row gets whatever the
   * memory copy of the same turn had (see ``loadCurrent``) rather than nothing, because
   * pressing 放大 walks this path and wiping the row is the 「放大把思考过程擦掉」 bug in
   * a new costume: 「画好了」 with the picture gone is a claim with nothing behind it.
   */
  artifacts: string[]
}

const seeded = ref<Line[]>([])
const consumed = ref(0)
const pending = ref<Attachment[]>([])
const overlay = ref<{ text: string; attachments: Attachment[] } | null>(null)
const fileRef = ref<HTMLInputElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)

const providers = ref<ProviderChoice[]>([])
const provider = ref('')
const model = ref('')
const thinking = ref('medium')
const turns = ref(10)
const tools = ref(true)
const thinkingLevels = ref<string[]>([])
/** 精确预算（设置面板里填的那个数）；档位是 custom 时显示要用。 */
const thinkingBudget = ref<number | null>(null)
const turnsBounds = ref<number[]>([])
const switching = ref(false)
const switchNotice = ref('')

/**
 * The collaboration: which shape, which chairs, how many times around.
 *
 * Seats are kept as pairs rather than as display strings because a provider name and a
 * model name are two lookups, and the first seat also decides who signs the answer --
 * which is exactly why the click order is visible to the operator.
 */
const mode = ref<CollaborationMode>('single')
const seats = ref<SeatChoice[]>([])
const rounds = ref(2)
const caps = ref<ModelCap[]>([])

const single = computed(() => mode.value === 'single')

/** Every model that can actually answer: a key on the row, or a row that needs none. */
const seatOptions = computed(() =>
  providers.value
    .filter((entry) => !needsKey(entry))
    .flatMap((entry) =>
      entry.models.map((spec) => {
        const cap = caps.value.find((row) => row.provider === entry.name && row.model === spec.id)
        return {
          key: `${entry.name}\u0000${spec.id}`,
          provider: entry.name,
          model: spec.id,
          label: `${entry.name}/${spec.id}`,
          // Three states, three sayings: 看不到图 is a measurement, the blank is silence.
          sight: cap?.vision === true ? '看图✓' : cap?.vision === false ? '看不到图' : '',
          vision: cap
            ? `上次实测：${cap.chat ? '能答' : '连不上'}${cap.vision === null ? '，图没测' : cap.vision ? '，看图可以' : '，不吃图'}（${cap.at}）`
            : '没测过：保存时测过的那次才算数',
        }
      }),
    ),
)

const requests = computed(() => requestsFor(mode.value, seats.value.length, rounds.value))

const modeTitle = computed(() => {
  const shape = single.value
    ? '一次问一个模型，工具和图片都在这条路上'
    : mode.value === 'table'
      ? '每轮每家看着前面所有话发一次言，最后一家合并'
      : mode.value === 'boss'
        ? '第一家拆任务、派活、收口；其余各家只看见分给自己的那一条'
        : '每家先独立答，再匿名互评，票最多的那份原话就是答案'
  return `${shape} · ${seats.value.length} 家 ≈ ${requests.value} 次请求（协作不带工具，也不带图）`
})

function picked(key: string): boolean {
  return seats.value.some((seat) => `${seat.provider}\u0000${seat.model}` === key)
}

/** Click a chair to sit, click again to leave. Order is the order they were clicked. */
function pickSeat(key: string): void {
  const option = seatOptions.value.find((entry) => entry.key === key)
  if (!option) return
  if (picked(key)) {
    seats.value = seats.value.filter((seat) => `${seat.provider}\u0000${seat.model}` !== key)
    return
  }
  if (seats.value.length >= MAX_SEATS) {
    localError.value = `一桌最多 ${MAX_SEATS} 家，多出来的没坐下`
    return
  }
  seats.value = [...seats.value, { provider: option.provider, model: option.model }]
}

function setRounds(event: Event): void {
  const value = Number((event.target as HTMLSelectElement).value)
  rounds.value = Number.isFinite(value) && value > 0 ? Math.min(value, MAX_ROUNDS) : 1
}

/** Switching shape keeps the chairs: the same three models are worth a table, a
 * dispatch and a vote, and re-picking them every time is busywork. */
function setMode(event: Event): void {
  const next = String((event.target as HTMLSelectElement).value) as CollaborationMode
  mode.value = MODE_ORDER.includes(next) ? next : 'single'
  if (!single.value && seats.value.length === 0) {
    // Start with whoever is already answering this tab, rather than an empty row of
    // buttons that has to be filled before the first question can be asked.
    const current = model.value
    seats.value = provider.value || current ? [{ provider: provider.value, model: current }] : []
  }
}

/** The picked provider's own row -- read once, because a second read can disagree with
 *  the first if the picker moves between the two. */
const currentRow = computed<ProviderChoice | undefined>(
  () => providers.value.find((entry) => entry.name === provider.value),
)

/** The models of the picked provider, and the level names as the backend spells them. */
const models = computed<ModelSpec[]>(() => currentRow.value?.models ?? [])

/** 自带那一行的模型表由「选哪个离线模型」决定，所以永远只有一张卡。以前"只有一个模型
 *  就不显示这一格"把它藏掉了，结果是屏上从头到尾只写着 'local'，看不出加载的是 0.6B
 *  还是 4B（2026-10-18：三条新问题被答成同一句原话，用户只能怀疑软件坏了）。 */
const modelRowLocked = computed(() => Boolean(currentRow.value?.models_locked))

/**
 * Friendly names for the four thinking levels. The wire values (`off`/`low`/...) are
 * what gets stored; these are only what the dropdown shows.
 */
const THINKING_LABELS: Record<string, string> = {
  off: '不思考',
  low: '轻度思考',
  medium: '中度思考',
  high: '深度思考',
}

/** 设置面板里填过精确预算时，档位对不上任何一档 —— 那就把那个数印出来。
 *  少这一项的话 `<select>` 挑不到 option，屏上看着像"这一对没设过"。 */
function levelLabel(level: string): string {
  if (level === 'custom') return `自定义 ${thinkingBudget.value ?? ''}`.trim()
  return THINKING_LABELS[level] ?? level
}

const thinkingTitle = computed(() => `思考强度：${levelLabel(thinking.value)}`)

/** The turn counts the dropdown offers: a few round numbers inside the allowed range. */
const turnOptions = computed<number[]>(() => {
  const [low, high] = turnsBounds.value
  if (turnsBounds.value.length !== 2) return []
  const steps = [0, 4, 8, 12, 20, 30, 50].filter((n) => n >= low && n <= high)
  if (!steps.includes(turns.value)) steps.push(turns.value)
  return [...new Set(steps)].sort((a, b) => a - b)
})

/**
 * The list is pulled, not configured in the page.
 *
 * Which providers and models exist is a fact about the operator's config file and
 * environment, and a second copy of that list in the bundle would be a list that
 * drifts the moment someone adds a provider.
 */
async function loadModels(): Promise<void> {
  try {
    const list = await fetchModels()
    providers.value = list.providers ?? []
    provider.value = list.provider
    model.value = list.model
    thinking.value = list.thinking
    thinkingBudget.value = list.thinking_custom_budget ?? null
    turns.value = list.turns
    tools.value = list.tools
    thinkingLevels.value = list.thinking_levels ?? []
    turnsBounds.value = list.turns_bounds ?? []
    caps.value = list.caps ?? []
  } catch {
    // No bridge yet: the picker simply does not appear, and the default model answers.
  }
}

/** What to say after a switch: the key gap is the part worth mentioning out loud -- and it
 * is only a gap when that row actually wants a credential. */
function describePick(): string {
  const row = providers.value.find((entry) => entry.name === provider.value)
  const spec = models.value.find((entry) => entry.id === model.value)
  if (!row || !spec) return '已切换模型'
  return needsKey(row)
    ? `已切到 ${spec.label}，但它缺 ${row.key_variable}，问话会失败`
    : `已切到 ${spec.label}`
}

/**
 * Picking a provider keeps the model when that vendor offers one by the same name and
 * otherwise lands on the vendor's default -- the backend decides which, so the page
 * never has to guess what a half-complete selection means.
 */
async function chooseProvider(event: Event): Promise<void> {
  const wanted = (event.target as HTMLSelectElement).value
  if (!wanted || wanted === provider.value) return
  await applyPick(wanted, '', event.target as HTMLSelectElement)
}

async function chooseModel(event: Event): Promise<void> {
  const wanted = (event.target as HTMLSelectElement).value
  if (!wanted || wanted === model.value) return
  await applyPick(provider.value, wanted, event.target as HTMLSelectElement)
}

async function applyPick(wanted: string, wantedModel: string, select: HTMLSelectElement): Promise<void> {
  switching.value = true
  try {
    const list = await pickModel(wanted, wantedModel)
    if (list.error) {
      // Put the select back where it actually is, rather than leaving it showing a
      // model that is not answering the next question.
      select.value = wantedModel ? model.value : provider.value
      switchNotice.value = `没能切过去：${list.error}`
      return
    }
    providers.value = list.providers ?? []
    provider.value = list.provider
    model.value = list.model
    thinking.value = list.thinking
    thinkingBudget.value = list.thinking_custom_budget ?? null
    turns.value = list.turns
    tools.value = list.tools
    switchNotice.value = describePick()
  } catch (err) {
    select.value = wantedModel ? model.value : provider.value
    switchNotice.value = err instanceof Error ? err.message : String(err)
  } finally {
    switching.value = false
  }
}

/**
 * Two knobs, one call each.
 *
 * Saving one must not reset the other, which is why the other argument is left out
 * rather than sent with its current value: the backend writes only what it is given,
 * and a page that echoed both would turn "raise the thinking budget" into a context
 * size the operator never picked.
 */
async function saveKnob(level?: string, count?: number, carry?: boolean): Promise<void> {
  switching.value = true
  try {
    const answer = await saveTuning(level, count, carry)
    if (!answer.ok) {
      // Put the two selects back where the knobs actually are. A `<select>` keeps
      // whatever was clicked, so a refusal left alone reads as "it saved" -- the
      // exact lie this picker was built to avoid.
      thinking.value = answer.thinking
      turns.value = answer.turns
      tools.value = answer.tools
      switchNotice.value = `没能保存：${answer.error}`
      return
    }
    thinking.value = answer.thinking
    turns.value = answer.turns
    tools.value = answer.tools
  } catch (err) {
    switchNotice.value = err instanceof Error ? err.message : String(err)
  } finally {
    switching.value = false
  }
}

async function saveThinking(event: Event): Promise<void> {
  await saveKnob((event.target as HTMLSelectElement).value, undefined)
}

async function saveTurns(event: Event): Promise<void> {
  await saveKnob(undefined, Number((event.target as HTMLSelectElement).value))
}

async function saveTools(event: Event): Promise<void> {
  await saveKnob(undefined, undefined, (event.target as HTMLInputElement).checked)
}


onMounted(() => {
  void loadModels()
  void loadCurrent()
  window.addEventListener('jarvis-settings-saved', onSettingsSaved)
  // 宠物那张卡上的 ＋ 按下去，壳子往这一页发这句。谁有输入框谁装这个钩子 —— 壳子因此
  // 不需要知道它在哪个元素上，宠物那一页（没有输入框）也不会接到就崩。
  shellCues().__jarvisFocusInput = focusInput
})

onBeforeUnmount(() => {
  window.removeEventListener('jarvis-settings-saved', onSettingsSaved)
  delete shellCues().__jarvisFocusInput
})

/** The cue names this page answers to from the shell, all optional. */
function shellCues(): { __jarvisFocusInput?: () => void } {
  return window as unknown as { __jarvisFocusInput?: () => void }
}

function focusInput(): void {
  inputRef.value?.focus()
}

/** The model list is whatever the settings panel last wrote; refetch, don't cache. */
function onSettingsSaved(): void {
  void loadModels()
}

async function loadCurrent(): Promise<void> {
  try {
    const list = await fetchSessions()
    sessions.value = list.sessions ?? []
    activeSession.value = list.current ?? ''
    if (list.error || !list.current) {
      seeded.value = []
      consumed.value = voice.history.length
      return
    }
    const stored = await fetchSessionMessages(list.current)
    // 思考过程不落盘（存的是问答）。以前这里给每一行写死空串，于是"重新读一遍"这个动作
    // 本身就把屏上刚看到的那段擦掉了 —— 点「放大」正是走这条路（打包版量到：收起时 216 字，
    // 放大后那一格整个没了）。落盘的只有问答，所以带过去只能靠"文字对得上"这一条：
    // 对得上的是同一轮，把内存里那份带上；对不上（真是重新打开的老对话）仍然留空，不假装存过。
    const previous = lines.value
    seeded.value = stored.messages.map((turn) => {
      const before = previous.find((row) => row.role === turn.role && row.text === turn.content)
      return {
        id: turn.id,
        role: turn.role,
        text: turn.content,
        reasoning: before?.reasoning ?? '',
        record: before?.record ?? '',
        model: before?.model ?? '',
        tools: before?.tools,
        attachments: turn.attachments ?? [],
        // 产出落不落盘是服务端的事，这一格只负责"重新读一遍不许把屏上那张擦掉"：
        // 文字对得上就是同一轮，把内存里那份带过去（和 reasoning 同一条判据）。
        artifacts: before?.artifacts ?? [],
      }
    })
    consumed.value = voice.history.length
  } catch {
    seeded.value = []
    consumed.value = voice.history.length
  }
  await scrollToEnd()
}

/**
 * The enlarged conversation view.
 *
 * 「放大」 moves *this* element over the whole window instead of opening a copy of it:
 * a second ChatPanel would have its own `seeded`/`consumed` pair, and two views of
 * one transcript that each decide where the stored part ends is exactly how the two
 * windows start disagreeing about what was said. One component, one state, two sizes.
 */
const expanded = ref(false)
const optionsOpen = ref(false)
const sessions = ref<SessionInfo[]>([])
const activeSession = ref('')
const railError = ref('')
const working = ref(false)

const activeTitle = computed(
  () => sessions.value.find((entry) => entry.id === activeSession.value)?.title ?? '当前会话',
)
const currentLabel = computed(() => {
  const spec = models.value.find((entry) => entry.id === model.value)
  if (!spec) return ''
  return provider.value ? `${provider.value} · ${spec.label}` : spec.label
})

async function toggleExpanded(): Promise<void> {
  expanded.value = !expanded.value
  if (expanded.value) {
    window.addEventListener('keydown', onEscape)
    await loadCurrent()
  } else {
    window.removeEventListener('keydown', onEscape)
  }
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape' && expanded.value) {
    expanded.value = false
    window.removeEventListener('keydown', onEscape)
  }
}

async function openSession(id: string): Promise<void> {
  if (id === activeSession.value || working.value) return
  working.value = true
  railError.value = ''
  try {
    await switchSession(id)
    await loadCurrent()
  } catch (err) {
    railError.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

async function createSession(): Promise<void> {
  if (working.value) return
  working.value = true
  railError.value = ''
  try {
    await newSession()
    await loadCurrent()
  } catch (err) {
    railError.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onEscape)
})

/**
 * The transcript is Python's, not this component's.
 *
 * Spoken turns arrive on the push channel, so a locally-owned list would show the
 * typed half of the conversation and drop the spoken half -- two histories in one
 * chat panel is a reader having to work out which one is real.
 */
/** One line under the bubble: what the request carried, in the words the backend chose. */
const lines = computed<Line[]>(() =>
  [
    ...seeded.value,
    ...voice.history.slice(consumed.value).map((turn) => ({
      role: turn.role,
      text: turn.text,
      reasoning: turn.reasoning ?? '',
      record: turn.record ?? '',
      model: turn.model ?? '',
      tools: turn.tools,
      attachments: turn.text === overlay.value?.text ? overlay.value.attachments : [],
      artifacts: turn.artifacts ?? [],
    })),
    // 只有图、没有配话的那一轮也要看得见：服务端现在容许它存在（`_add_turn` 里那句
    // `if not text` 会把它整轮擦掉，改成了「有产出就留」），页面这一头的过滤器不能
    // 接着当第二道闸 —— 少这一条，画完一句话都没说的那张图就在屏上凭空消失。
  ].filter((line) => line.text.trim() !== '' || line.artifacts.length > 0),
)
const error = computed(() => localError.value || voice.error)
const notice = computed(() => voice.notice)
/** A reply to the last click wins over the ambient status line, then fades. */
const shownNotice = computed(() => switchNotice.value || notice.value)
const listening = computed(() => voice.turn === 'listening')

/**
 * 这一轮产出的文件：一张图问一次，每一颗按钮的结果都落在屏上。
 *
 * 三份状态各有各的去处 —— 图的数据在 ``pictureCache``（模块级，见文件顶上的 `<script>`）
 * 加一份 reactive 的镜像给模板用；读图失败的话**不消失**，因为「这张没画出来」和「这一轮
 * 还在读」在屏上必须是两件事；按钮那一句结果 2.6 秒后退回按钮本名，不许常驻。
 */
const pictures = ref<Record<string, string>>({})
const videos = ref<Record<string, string>>({})
const videoAsked = new Set<string>()
const videoPaths = computed(() => lines.value.flatMap(line => line.artifacts).filter(path => suffixOf(path) === '.mp4'))
watch(() => videoPaths.value.join('\n'), () => {
  for (const path of videoPaths.value) {
    if (videoAsked.has(path)) continue
    videoAsked.add(path)
    artifactVideo(path).then(result => { if (result.ok) videos.value[path] = result.data_url }).catch(() => {})
  }
}, { immediate: true })
const pictureFails = ref<Record<string, string>>({})
const flashes = ref<Record<string, string>>({})
const bubbleMarks = ref<Record<number, CopyOutcome>>({})
const flashTimers: Record<string, number> = {}

/** 和图片那一档扩展名 —— 和 `jarvis.ui.desktop` 的 `_ARTIFACT_IMAGE_SUFFIXES` 是同一批值。 */
const PICTURE_SUFFIXES = ['.png', '.jpg', '.jpeg', '.webp']

function suffixOf(path: string): string {
  const tail = String(path || '').split(/[\\/]/).pop() ?? ''
  const dot = tail.lastIndexOf('.')
  return dot > 0 ? tail.slice(dot).toLowerCase() : ''
}

function isPicture(path: string): boolean {
  return PICTURE_SUFFIXES.includes(suffixOf(path))
}

/** 给人看的是文件名，不是整条路径；路径那一格有专门的「复制路径」。 */
function baseName(path: string): string {
  return String(path || '').split(/[\\/]/).pop() ?? String(path || '')
}

function pictureOf(path: string): string {
  return pictures.value[path] ?? ''
}

/** 还在读 / 读不出来 —— 两个都得说得出名字，不能都画成"这里什么都没有"。 */
function waitWords(path: string): string {
  return pictureFails.value[path] ?? '这一张还在读…'
}

/**
 * 只在"这一屏第一次见到这条路径"时敲一次门。
 *
 * 触发器是路径列表拼出来的一个字符串，不是路径数组本身：`lines` 每 1.5 秒就是一次新数组，
 * watch 那个数组等于每 1.5 秒重跑一遍 —— 读盘的那扇门就是这么被敲成轮询的。
 */
const picturePaths = computed<string[]>(() => {
  const found = new Set<string>()
  for (const line of lines.value) {
    if (line.role !== 'assistant') continue
    for (const path of line.artifacts) if (isPicture(path)) found.add(path)
  }
  return [...found]
})
const pictureKey = computed(() => picturePaths.value.join('\n'))

function askPicture(path: string): void {
  if (pictureAsked.has(path)) return
  pictureAsked.add(path)
  const cached = pictureCache.get(path)
  if (cached) {
    pictures.value = { ...pictures.value, [path]: cached }
    return
  }
  artifactRead(path)
    .then((answer) => {
      if (answer.ok && answer.data_url) {
        pictureCache.set(path, answer.data_url)
        pictures.value = { ...pictures.value, [path]: answer.data_url }
        return
      }
      pictureFails.value = { ...pictureFails.value, [path]: answer.error || '这一张图没读出来' }
    })
    .catch((err: unknown) => {
      pictureFails.value = {
        ...pictureFails.value,
        [path]: err instanceof Error ? err.message : String(err),
      }
    })
}

watch(pictureKey, () => { for (const path of picturePaths.value) askPicture(path) }, { immediate: true })

/** 一句结果挂多久：按钮要给人看清，但不许变成屏上一道擦不掉的疤。 */
const FLASH_MS = 2600

const ART_LABELS: Record<string, string> = {
  'copy-image': '复制图片',
  'copy-path': '复制路径',
  open: '打开',
  folder: '所在文件夹',
}

const ART_OK: Record<string, string> = {
  'copy-image': '图已经在剪贴板里了',
  'copy-path': '已复制',
  open: '交给了系统',
  folder: '打开了它所在的位置',
}

/** 一颗按钮的临时结果，2.6 秒后自己退回去。读图失败不是这一类 —— 那一格不许消失。 */
function flash(key: string, text: string): void {
  flashes.value = { ...flashes.value, [key]: text }
  window.clearTimeout(flashTimers[key])
  flashTimers[key] = window.setTimeout(() => {
    const next = { ...flashes.value }
    delete next[key]
    flashes.value = next
  }, FLASH_MS)
}

/** 这一件东西上最近一句要说的话 —— 一个槽，几颗按钮共用，谁最后响谁说。 */
function artWhy(path: string): string {
  for (const action of ['copy-image', 'copy-path', 'open', 'folder']) {
    const text = flashes.value[`${action}|${path}`]
    if (text) return `${ART_LABELS[action]}：${text}`
  }
  return ''
}

/**
 * 一扇门一次点击：成了报成，没成把服务端那句话原样贴出来。
 *
 * 这句不许吞：`ok: false` 带的是一个能照着修的原因（「只能复制软件自己产出的文件」/
 * 「这个路径不是软件画出来的图片」），写进 console 就等于没有 —— 用户不会开控制台，
 * 更不会在两个人都看着的屏幕上找错误。
 */
async function runDoor(action: string, path: string, call: () => Promise<ActionResult>): Promise<void> {
  try {
    const answer = await call()
    flash(`${action}|${path}`, answer.ok ? (ART_OK[action] ?? '成了') : answer.error || '这一扇没答应')
  } catch (err) {
    flash(`${action}|${path}`, err instanceof Error ? err.message : String(err))
  }
}

function copyPicture(path: string): Promise<void> {
  return runDoor('copy-image', path, () => copyImage(path))
}

function openArt(path: string, how: ArtifactHow): Promise<void> {
  return runDoor(how === 'folder' ? 'folder' : 'open', path, () => openArtifact(path, how))
}

/** 抄的是那串路径本身，所以先走浏览器那两行代码，它说没成才敲剪贴板那扇门。 */
function copyPath(path: string): Promise<void> {
  return copyViaBothWays('copy-path', path)
}

/**
 * 先 `navigator.clipboard`，报 `failed` 才走 `copy_text` 那扇门。
 *
 * 顺序是这个项目定下来的：浏览器那条在打包版里常常就是没权限，而 Windows 剪贴板那一条
 * 永远可用；反过来先敲门，就等于每次复制都过一次进程间调用。两种都没成才写「没复制上」，
 * 并且把门那句原因挂在旁边。
 */
async function copyViaBothWays(action: string, value: string): Promise<void> {
  const outcome = await copyText(value)
  if (outcome === 'copied') {
    flash(`${action}|${value}`, ART_OK[action] ?? '已复制')
    return
  }
  if (outcome === 'empty') {
    flash(`${action}|${value}`, '没有内容可复制')
    return
  }
  try {
    const answer = await copyTextViaBridge(value)
    flash(`${action}|${value}`, answer.ok ? (ART_OK[action] ?? '已复制') : answer.error || '没复制上')
  } catch (err) {
    flash(`${action}|${value}`, err instanceof Error ? err.message : String(err))
  }
}

const COPY_MARKS: Record<CopyOutcome, string> = {
  copied: '已复制',
  empty: '没有内容',
  failed: '没复制上',
}

/** 没按过时这一格就是按钮本名 —— 给它一个默认的"已复制"是假动作的教科书写法。 */
function copyMark(index: number): string {
  const outcome = bubbleMarks.value[index]
  return outcome ? COPY_MARKS[outcome] : '复制'
}

/** 那句原因的槽和按钮同名：`copy|<行号>`，由 `copyBubble` 写。 */
function copyWhy(index: number): string {
  return flashes.value[`copy|${index}`] ?? ''
}

/** 按钮本名过一会儿自己回来；行号会跟着新的一轮往下挪，所以旧的自己退场。 */
const markTimers: Record<number, number> = {}

function setMark(index: number, outcome: CopyOutcome): void {
  bubbleMarks.value = { ...bubbleMarks.value, [index]: outcome }
  window.clearTimeout(markTimers[index])
  markTimers[index] = window.setTimeout(() => {
    const next = { ...bubbleMarks.value }
    delete next[index]
    bubbleMarks.value = next
    delete markTimers[index]
  }, FLASH_MS)
}

/** 气泡这段话：结果写在按钮本身上，失败的原因挂在它右边。 */
async function copyBubble(index: number, value: string): Promise<void> {
  const outcome = await copyText(value)
  if (outcome === 'copied') {
    setMark(index, 'copied')
    return
  }
  if (outcome === 'empty') {
    setMark(index, 'empty')
    flash(`copy|${index}`, '这一段没有话，只有产出')
    return
  }
  // 浏览器那条路报的是 failed：这一轮还没有输，敲 Windows 剪贴板那一扇门。
  try {
    const answer = await copyTextViaBridge(value)
    setMark(index, answer.ok ? 'copied' : 'failed')
    if (!answer.ok) flash(`copy|${index}`, answer.error || '剪贴板那扇门也没答应')
  } catch (err) {
    setMark(index, 'failed')
    flash(`copy|${index}`, err instanceof Error ? err.message : String(err))
  }
}

/* ------------------------------------------------------------ 右键菜单 */

const { openMenu } = useContextMenu()

/**
 * 附件卡片第二行那句"多大 · 什么时候写的"。
 *
 * 只有路径时这一格只能显示扩展名，而人挑文件靠的是"这是我刚那份吗"。`artifact_stat`
 * 和读图走同一道收束（不在软件自己那两个产出目录里就不给），并且**一次问一条、问过
 * 不再问** —— 这一屏 1.5 秒重画一次，不能把读盘敲成轮询。
 */
const stats = ref<Record<string, string>>({})
const statsAsked = new Set<string>()

function fileSub(path: string): string {
  return stats.value[path] ?? extLabel(path)
}

/** 角标上那三个字母：`.xlsx` 太长了，而且点号在图标里没有意义。 */
function extLabel(path: string): string {
  return suffixOf(path).replace('.', '').toUpperCase()
}

function askStat(path: string): void {
  if (statsAsked.has(path)) return
  statsAsked.add(path)
  const fallback = (): void => {
    stats.value = { ...stats.value, [path]: extLabel(path) }
  }
  artifactStat(path)
    .then((answer) => {
      if (!answer.ok) {
        fallback()
        return
      }
      stats.value = {
        ...stats.value,
        [path]: `${formatBytes(answer.size)} · ${dayTime(answer.mtime)}`,
      }
    })
    .catch(fallback)
}

/** 卡片上那句短日期：全时间戳留给对话流的分段，这一格要短到不换行。 */
function dayTime(epoch: number): string {
  if (!epoch) return ''
  const date = new Date(epoch * 1000)
  const now = new Date()
  const clock = `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
  if (date.toDateString() === now.toDateString()) return `今天 ${clock}`
  if (date.toDateString() === new Date(now.getTime() - 86400000).toDateString()) return `昨天 ${clock}`
  return `${date.getMonth() + 1}-${String(date.getDate()).padStart(2, '0')} ${clock}`
}

const filePaths = computed<string[]>(() =>
  lines.value.flatMap((line) =>
    line.role === 'assistant' ? line.artifacts.filter((path) => !isPicture(path)) : [],
  ),
)
watch(filePaths, (paths) => { for (const path of paths) askStat(path) }, { immediate: true })

/** 选中的字优先：拖了三分之一句却复制回整段回答，是越帮越忙。 */
function pickedText(): string {
  return (document.getSelection()?.toString() ?? '').trim()
}

async function copyAnywhere(value: string): Promise<void> {
  await copyViaBothWays('copy-any', value)
}

function textMenu(event: MouseEvent, index: number, text: string): void {
  const picked = pickedText()
  const entries: MenuEntry[] = []
  if (picked) entries.push({ label: '复制选中文字', run: () => copyAnywhere(picked) })
  const line = lines.value[index]
  if (text.trim()) entries.push({ label: line?.role === 'user' ? '复制这个提问' : '复制这段回答', run: () => copyBubble(index, text) })
  if (line?.role === 'user') entries.push({ label: '修改并重新提问', run: () => editQuestion(index) })
  if (!entries.length) entries.push({ label: '这一段没有内容', run: () => {}, disabled: true })
  openMenu(event, entries)
}

function textParts(text: string): { text: string; url?: string }[] {
  const parts: { text: string; url?: string }[] = []
  let cursor = 0
  for (const match of text.matchAll(/https?:\/\/[^\s<>）)\]"']+/g)) {
    const index = match.index ?? 0
    const url = match[0].replace(/[，。；,.;]+$/, '')
    if (index > cursor) parts.push({ text: text.slice(cursor, index) })
    parts.push({ text: url, url })
    cursor = index + url.length
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) })
  return parts
}

async function editQuestion(index: number): Promise<void> {
  const line = lines.value[index]
  const conversation = activeTab.value?.id || activeSession.value
  if (!line || line.role !== 'user' || !conversation) return
  try {
    const stored = await fetchSessionMessages(conversation)
    const ordinal = lines.value.slice(0, index + 1).filter(row => row.role === 'user').length - 1
    const original = line.id ? stored.messages.find(row => row.id === line.id) : stored.messages.filter(row => row.role === 'user')[ordinal]
    if (!original?.id || original.content !== line.text) throw new Error('提问尚未落盘或记录已更新，请稍后重试')
    editing.value = { conversation, id: original.id }
    draft.value = original.content
    pending.value = [...(original.attachments || [])]
    mode.value = 'single'
    await nextTick()
    inputRef.value?.focus()
  } catch (error) {
    localError.value = String(error)
  }
}

/**
 * 一个对象一个菜单（微信那条规矩）：图、文件都从这一处出，那几颗按钮只是它的快捷方式。
 * 打包版没有原生右键菜单可退 —— pywebview 把 `AreDefaultContextMenusEnabled` 绑在
 * debug 上（edgechromium.py:288），不自己做一个，右键就是"鼠标没反应"。
 */
function artMenu(event: MouseEvent, path: string): void {
  const entries: MenuEntry[] = []
  if (isPicture(path)) entries.push({ label: '复制图片', run: () => copyPicture(path) })
  entries.push({ label: '打开', run: () => openArt(path, 'open') })
  entries.push({ label: '所在文件夹', run: () => openArt(path, 'folder') })
  entries.push({ separator: true })
  entries.push({ label: '复制文件路径', run: () => copyPath(path) })
  openMenu(event, entries)
}

onBeforeUnmount(() => {
  for (const timer of Object.values(flashTimers)) window.clearTimeout(timer)
  for (const timer of Object.values(markTimers)) window.clearTimeout(timer)
})

/**
 * The tabs, and the one being read.
 *
 * Read from the pushed snapshot rather than kept locally: two conversations can be
 * answering at once, and a strip that remembered its own idea of which tab was busy
 * would be wrong the moment the other one finished first.
 */
const tabs = computed<ConversationCard[]>(() => voice.conversations)
const activeTab = computed<ConversationCard | undefined>(() =>
  tabs.value.find((card) => card.active),
)
/** Every provider/model pair this window knows, as pickable rows for one conversation. */
const conversationModelChoices = computed(() =>
  providers.value.flatMap((entry) =>
    entry.models.map((spec) => ({
      key: `${entry.name}\u0000${spec.id}`,
      label: `${entry.name} · ${spec.label}`,
      provider: entry.name,
      model: spec.id,
    })),
  ),
)
const activeTabModelKey = computed(() => {
  const tab = activeTab.value
  if (!tab) return ''
  const hit = conversationModelChoices.value.find(
    (pair) => pair.provider === tab.provider && pair.model === tab.model,
  )
  return hit ? hit.key : `${tab.provider}\u0000${tab.model}`
})
const pinNote = ref('')

/** Point just this tab at another model; the others keep theirs. */
async function pinConversationModel(event: Event): Promise<void> {
  const tab = activeTab.value
  if (!tab) return
  const key = (event.target as HTMLSelectElement).value
  const pair = conversationModelChoices.value.find((item) => item.key === key)
  if (!pair) return
  pinNote.value = ''
  const answer = await pickConversationModel(tab.id, pair.provider, pair.model)
  if (!answer.ok) pinNote.value = answer.error || '没换成'
}
/** The active tab's in-flight turn, which is the only one 停止 can point at. */
const runningTask = computed(() => (activeTab.value?.status === 'running' ? activeTab.value.task_id : ''))

/** What this tab has typed but not asked yet, in order. Empty almost always. */
const queued = computed<QueuedQuestion[]>(() => activeTab.value?.queued ?? [])

/** 气泡上那行小字：工具中 / 输出中 / 某家在发言。空就意味着只有「思考中」。 */
const thinkingPhase = computed(() => {
  const phase = String(activeTab.value?.phase || '')
  return phase && phase !== '思考中' ? phase : ''
})

/** Which of the four shapes the shell asked for. Unknown values fall back to dots. */
const loaderKind = computed(() => {
  const wanted = String(voice.thinkingLoader || 'dots')
  return LOADER_KINDS.includes(wanted) ? wanted : 'dots'
})

/** The answer still arriving in this tab, or null for a tab that is only thinking. */
const streamed = computed(() => {
  const streaming = voice.streaming
  if (!streaming || streaming.conversation_id !== activeTab.value?.id) return null
  return streaming
})

/**
 * 这一家失败之后该做什么。键是报错自带的 kind（``jarvis.llm.errors`` 里写死的分类），
 * 不是报错的句子 —— 句子给人抄，分类给机器读，两者混在一起改文案就会把提示改哑。
 */
const FAILURE_HINTS: Record<string, string> = {
  auth: 'Key 被拒了：打开 设置 · 服务商，把这一行的 Key 重填一次（注意填的是值，不是变量名）',
  forbidden: '这把 Key 能用，但它不许问这个模型（403）：换个模型名，或去厂商那边给这个令牌开这个模型',
  quota: '这一家的额度或频率到顶了：等它恢复，或在 设置 · 服务商 换一家再问',
  timeout: '这一家答得太慢被掐了：在 设置 · 服务商 把这一行的超时调大一点',
  network:
    '连不上这个地址：先看服务起没起、端口对不对，再看防火墙/代理（报错里括号内就是它要连的那个端口）',
  server: '厂商自己返回 5xx：不是你这边的设置，稍后再试或换一家',
  request: '这一家不收这个请求：模型名写错，或这句话（含图片）太长了',
  overflow:
    '这一家的上下文装不下这一句（工具表实测就要 4639 token）：把「带工具」勾掉、把「上下文轮数」调小，或者把服务端的上下文（n_ctx / num_ctx）调大',
  malformed: '它回的内容读不成聊天回复：这个地址多半不是 OpenAI 兼容接口',
  empty: '它应了但什么都没返回：再问一次，或换一家',
  crash: '这是我这边的毛病：logs/jarvis.log 里有完整堆栈',
  pictures: '协作这一桌不带你这张图：把图单独问某一个模型',
}

/**
 * 这一轮做了什么答案里看不出来的事。键是后端给的 token（``Conversation.note``），
 * 句子在这里 —— 和失败卡同一个道理：改文案不能把说明改哑。
 */
const NOTE_LABELS: Record<string, string> = {
  no_tools: '这一句是去掉工具表才装进它的上下文的：这一轮她只回答，动不了这台电脑。',
}

/** 只有真答上了一轮才说这句；还在跑、或者根本没答上，都不该冒出来。 */
const turnNote = computed(() => {
  const tab = activeTab.value
  if (!tab || tab.status !== 'done' || runningTask.value) return ''
  return NOTE_LABELS[String(tab.note || '')] ?? ''
})

/**
 * 后端替这一对把工具表扔了，那一格就得跟着改。
 *
 * 不等下一次刷新：屏上写着「带工具」而请求里根本没有，正是这一轮刚发生过的事 —— 操作员
 * 会照着屏上那句话去说"帮我打开记事本"，然后等到一句她其实没动手的回答。
 */
watch(
  () => activeTab.value?.note ?? '',
  (note) => {
    if (note === 'no_tools') void loadModels()
  },
)

/** 这句没答上时该说什么。没在失败、或这一轮还在跑，都不该出现这张卡片。 */
const failure = computed(() => {
  const tab = activeTab.value
  if (!tab || tab.status !== 'failed' || runningTask.value) return null
  const raw = String(tab.error || '').trim()
  // 报错形如 "消息 (provider='x', status=401, body='...')"：正文单独一行，细节折起来。
  const cut = raw.indexOf(' (')
  const reason = raw ? (cut > 4 ? raw.slice(0, cut) : raw) : '这一轮没留下原因，日志里也没有'
  return {
    reason,
    detail: cut > 4 ? raw : '',
    hint: FAILURE_HINTS[String(tab.error_kind || '')] ?? '',
    where: tab.provider ? `问的是：${tab.provider}${tab.model ? ` · ${tab.model}` : ''}` : '',
  }
})

const talkTitle = computed(() => {
  if (voice.enabled) return '跳过唤醒词，直接说这一句'
  return voice.label
})

/** Whether the transcript is being followed. Off the moment the operator scrolls up:
 *  a log that yanks the view down while somebody is re-reading an earlier answer is
 *  worse than one that does not follow at all. */
const following = ref(true)

function onLogScroll(): void {
  const element = listRef.value
  if (!element) return
  // A few pixels of slack: the last row's own padding and a sub-pixel scroll offset
  // should not read as "they went browsing".
  const gap = element.scrollHeight - element.scrollTop - element.clientHeight
  following.value = gap < 24
}

/** Pin to the newest row, but only if that is where the operator was already looking. */
function followToEnd(): void {
  if (!following.value) return
  const element = listRef.value
  if (element) element.scrollTop = element.scrollHeight
}

async function scrollToEnd(): Promise<void> {
  await nextTick()
  const element = listRef.value
  if (element) {
    element.scrollTop = element.scrollHeight
    following.value = true
  }
}

/**
 * How much transcript is on screen, as a cheap string: stored rows, whether the
 * 「思考中」 card is drawn, and the answer as it streams in.
 *
 * Watching this instead of the DOM keeps an observer out of the render loop, and it
 * catches the case that made the panel feel broken: the question goes out, the thinking
 * card appears *below* the fold, and nothing moved -- so it looks like she never started.
 */
const logExtent = computed(() => {
  const thinking = Boolean(runningTask.value) || voice.turn === 'processing'
  return [
    lines.value.length,
    thinking && !streamed.value?.text ? 1 : 0,
    streamed.value?.text.length ?? 0,
  ].join(':')
})

watch(logExtent, () => {
  void nextTick(followToEnd)
})

async function talk(): Promise<void> {
  if (!voice.enabled) {
    await voice.enable()
    if (!voice.enabled) return
  }
  await voice.talk()
  await scrollToEnd()
}

function openFiles(): void {
  fileRef.value?.click()
}

function tabTitle(tab: ConversationCard): string {
  const model = tab.model ? ` · ${tab.model}` : tab.provider ? ` · ${tab.provider}` : ''
  const asked = tab.turns ? ` · ${tab.turns} 问` : ''
  return `${tab.title || '新对话'}${model}${asked}${tab.status === 'running' ? ` · ${tab.phase}` : ''}`
}

/**
 * Paste an image straight from the clipboard.
 *
 * The same reader the 附件 button uses, because a clipboard PNG is routinely several
 * megabytes and the model context wants a thumbnail, not the original. Anything that
 * is not an image is refused here rather than carried in as a name: the assistant cannot
 * open a file, and a chip that implies otherwise is a lie with a thumbnail on it.
 */
async function onPaste(event: ClipboardEvent): Promise<void> {
  const files = Array.from(event.clipboardData?.files ?? [])
  const images = files.filter((file) => file.type.startsWith('image/'))
  if (images.length === 0) return
  // Stop the browser from pasting a multi-megabyte base64 blob into the text input.
  event.preventDefault()
  for (const file of images) {
    if (pending.value.length >= 4) {
      localError.value = '一条问题最多带 4 个附件，后面的没加进来'
      break
    }
    pending.value.push(await readAttachment(named(file, '粘贴图片')))
  }
  await scrollToEnd()
}

function named(file: File, fallback: string): File {
  return file.name ? file : new File([file], `${fallback}.${file.type.split('/')[1] ?? 'png'}`, { type: file.type })
}

/** Read one picked file: images shrunk to what she can receive, everything else as facts. */
async function readAttachment(file: File): Promise<Attachment> {
  const kind: Attachment['kind'] = file.type.startsWith('image/')
    ? 'image'
    : file.type.startsWith('video/')
      ? 'video'
      : 'file'
  const base: Attachment = {
    name: file.name,
    kind,
    mime: file.type || 'application/octet-stream',
    size: file.size,
  }
  if (kind !== 'image') return base
  const raw = await readAsDataUrl(file)
  if (!raw.startsWith('data:image/')) {
    localError.value = `${base.name}：读不出图片内容，没有随问题发出去`
    return base
  }
  try {
    return { ...base, data: await shrinkToBudget(raw) }
  } catch (error) {
    // No data URL at all rather than a big one: the backend drops an oversize picture
    // silently, so a chip that looks attached would promise a picture she never gets.
    localError.value = `${base.name}：${error instanceof Error ? error.message : String(error)}`
    return base
  }
}

async function onFiles(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  for (const file of files) {
    if (pending.value.length >= 4) break
    pending.value.push(await readAttachment(file))
  }
}

/**
 * Ask, and stop waiting.
 *
 * ``chatAsk`` used to block until the answer existed, which is why the whole exchange
 * appeared at once: the question was only put on screen in the code that ran *after*
 * the reply came back. Now the shell shows the question as soon as the turn starts and
 * streams the answer into a temporary bubble, so the wait has a shape and a stop button.
 */
async function send(): Promise<void> {
  const question = draft.value.trim()
  if (question === '') return
  const attachments = [...pending.value]
  if (!single.value && seats.value.length === 0) {
    localError.value = '还没人选：在上面点几家模型再问'
    return
  }
  if (mode.value === 'boss' && seats.value.length < 2) {
    localError.value = '主管分发至少要两把椅子：一个拆，一个干活'
    return
  }
  if (!single.value && attachments.length) {
    // The backend refuses the same combination; saying it here keeps the question and the
    // picture in the box instead of bouncing a half-sent turn off the bridge.
    localError.value = '协作模式不带图。要问这张图，把形态换回「单个模型」再发。'
    return
  }
  draft.value = ''
  pending.value = []
  overlay.value = attachments.length ? { text: question, attachments } : null
  localError.value = ''
  await scrollToEnd()
  try {
    // 这一页钉过模型的会话，发出去的就是钉住的那一对：顶栏那两个 ref 是**全局**选择，
    // 照发会把 pin 顶掉 —— 屏上三处写着 deepseek、实际花钱问的是 qwen（r86 审计抓到的）。
    const tab = activeTab.value
    const pinned = Boolean(tab?.provider || tab?.model)
    const started = editing.value
      ? await chatEditQuestion(editing.value.conversation, editing.value.id, question, attachments)
      : single.value
      ? await chatSend(
          question,
          attachments,
          tab?.id ?? '',
          pinned ? (tab?.provider ?? '') : provider.value,
          pinned ? (tab?.model ?? '') : model.value,
        )
      : await chatCollaborate(
          question,
          seats.value,
          mode.value,
          rounds.value,
          activeTab.value?.id ?? '',
        )
    if (started.error) {
      // Keep the question in the box: it is the only record of what was attempted.
      localError.value = started.error
      draft.value = question
      overlay.value = null
      return
    }
    if (editing.value) {
      editing.value = null
      await openConversation(started.conversation)
      await loadCurrent()
    }
    // One pull, because nothing else may push: a quiet microphone has no reason to
    // change state, and the question we just asked is already Python's to report.
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
    draft.value = question
    overlay.value = null
  } finally {
    await scrollToEnd()
  }
}

/** Stop this tab's turn. Other tabs keep theirs -- that is the point of the id. */
async function stop(): Promise<void> {
  const task = runningTask.value
  if (!task) return
  try {
    const answer = await chatCancel(task)
    if (!answer.ok) localError.value = answer.error
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

/**
 * Take a waiting question back out. She never asked it, so nothing has to be undone:
 * a queued turn has no thread, no transcript row and no spend behind it.
 */
async function withdraw(taskId: string): Promise<void> {
  try {
    const answer = await chatCancel(taskId)
    if (!answer.ok) localError.value = answer.error
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

/**
 * 「回复」= put the cursor in the box.
 *
 * Not a different kind of message: what you type next is an ordinary question, and the
 * task table decides whether it runs now or waits. That is why this button is worth
 * having while she is busy rather than only when she is not.
 */
function askBack(): void {
  inputRef.value?.focus()
}

async function openTab(conversationId: string): Promise<void> {
  if (conversationId === activeTab.value?.id) return
  try {
    await openConversation(conversationId)
    seeded.value = []
    consumed.value = 0
    localError.value = ''
    await loadCurrent()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

async function addTab(): Promise<void> {
  try {
    await newConversation()
    seeded.value = []
    consumed.value = 0
    overlay.value = null
    localError.value = ''
    await voice.pullSnapshot()
    // chat_new only pushes the conversation list. The history snapshot can still
    // hold the previous tab, so an empty new tab would render the old answers
    // until the next switch. Reload the now-active (empty) conversation.
    await loadCurrent()
    await scrollToEnd()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

async function exportChat(): Promise<void> {
  const conversation = activeTab.value?.id || activeSession.value
  if (!conversation) return
  try {
    const answer = await exportConversation(conversation)
    switchNotice.value = answer.ok
      ? `已导出：${answer.path}`
      : `没能导出：${answer.error || '未知原因'}`
  } catch (err) {
    switchNotice.value = `没能导出：${err instanceof Error ? err.message : String(err)}`
  }
}

async function clear(): Promise<void> {
  // 清空 opens a fresh conversation on the server; the old one stays stored and
  // reachable from 历史. Wiping it here would make the button mean two things.
  await voice.clearTranscript()
  localError.value = ''
  await loadCurrent()
}
</script>

<style scoped>
/* Sizing and shape come from .hud-field in hud.css so this sits at exactly the
   same height as the buttons either side of it. */

/*
 * The tab strip. A dim dot means idle and a lit one means that conversation has a turn
 * in flight -- the one thing the strip has to say, since the panel below only shows the
 * tab you are reading.
 */
.chat__tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 0 0 6px;
}

.chat__tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 190px;
  font-size: 11px;
  opacity: 0.72;
}

.chat__tab--active {
  opacity: 1;
  border-color: var(--hud-cyan);
}

.chat__tab--busy {
  opacity: 1;
}

.chat__tab--new {
  max-width: none;
  padding: 0 8px;
}

.chat__tab-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--hud-dim, #4a5560);
  flex: none;
}

.chat__tab--busy .chat__tab-dot {
  background: var(--hud-amber, #e8a33d);
  animation: chat-tab-pulse 1.2s ease-in-out infinite;
}

.chat__tab-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat__tab-phase {
  color: var(--hud-cyan);
  font-size: 10px;
}

@keyframes chat-tab-pulse {
  50% {
    opacity: 0.35;
  }
}

/* The answer arriving: a caret rather than dots, so "still coming" reads differently
   from "has not started". */
.chat__caret::after {
  display: inline-block;
  margin-left: 2px;
  content: '▍';
  color: var(--hud-cyan);
  animation: chat-tab-pulse 1s ease-in-out infinite;
}

.chat__model {
  color: var(--hud-cyan);
  font-size: 11px;
  max-width: 150px;
  flex: none;
}

.chat__model:disabled {
  opacity: 0.45;
}

/* The two per-model knobs sit beside the pickers but read dimmer: they change *how*
   the picked model answers, not *which* one does. */
.chat__knob {
  color: var(--hud-dim);
  font-size: 11px;
  max-width: 104px;
  flex: none;
}

.chat__knob:disabled {
  opacity: 0.45;
}

.chat__knob--check {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  max-width: none;
}

.chat__knob--check input {
  accent-color: var(--hud-cyan);
}

/*
 * One column normally; two when enlarged. The rail is a v-if'd sibling, so inline
 * the grid collapses to a single track and `.chat__main` carries the column of
 * header/log/form exactly as before -- the enlarged view adds a column, it does not
 * restyle the conversation.
 */
.chat {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  padding: 12px 14px;
  min-height: 0;
}

.chat__main {
  /* 放大态那颗「关闭」的定位上下文（见模板里 chat__close 那段注释）。 */
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  min-width: 0;
}

/*
 * 右上角，和其余弹窗的「关闭」同一个位置。top 用 1px 而不是 0：header 那行是
 * 10.5px 的字配 24px 的按钮，贴顶会高出半格。
 */
.chat__close {
  position: absolute;
  top: 1px;
  right: 0;
  z-index: 2;
}

/*
 * 放大 lifts this one element over the whole window. Fixed positioning rather than a
 * second component: see the script. The scrim is a box-shadow spread wide enough to
 * darken everything behind it, because a dialog that leaves the dashboard fully
 * readable behind it is not a dialog, it is a second dashboard.
 */
.chat--max {
  position: fixed;
  inset: 18px;
  /*
   * 39, not 46: 放大 is a panel, not a dialog on top of dialogs. Every popup in this
   * window sits at 40 (the assistant's at 44), and while the chat was at 46 clicking
   * 用量 / 历史 / 音色 / 设置 with the chat expanded opened them *underneath* it -- the
   * button looked dead. One number below the popups keeps the scrim over the dashboard
   * and the popups over the scrim.
   */
  z-index: 39;
  grid-template-columns: 216px minmax(0, 1fr);
  gap: 14px;
  padding: 14px 16px;
  /*
   * Nearly solid, unlike the inline panel. The panel's normal scrim lets the
   * dashboard read through by design, which is right for a 150px-tall strip and
   * wrong for a conversation you are supposed to read: the trend chart's grid lines
   * came through the bubbles and looked like part of the answer.
   */
  background: linear-gradient(180deg, rgba(9, 22, 38, 0.985), rgba(3, 7, 13, 0.99));
  box-shadow: 0 0 0 2000px rgba(2, 5, 10, 0.72), var(--hud-elev-2);
}

.chat__rail {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 0;
  padding-right: 12px;
  border-right: 1px solid var(--hud-line);
}

.chat__rail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.chat__rail-list {
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

.chat__session {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 6px 10px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
  background: rgba(77, 216, 255, 0.04);
  color: var(--hud-text);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}

.chat__session:hover {
  border-color: var(--hud-rim);
}

.chat__session--on {
  background: rgba(77, 216, 255, 0.14);
  border-color: rgba(77, 216, 255, 0.42);
}

.chat__session-title {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.chat__session-meta {
  font-size: 10px;
  color: var(--hud-dim);
}

.chat__rail-error {
  margin: 0;
  font-size: 11px;
  color: var(--hud-red);
}

.chat__foot {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--hud-line);
}

/* The enlarged view is where a long read happens, so the type is bigger by design. */
.chat--max .chat__turn {
  font-size: 13px;
  line-height: 1.75;
}

.chat--max .chat__bubble {
  max-width: 76%;
  padding: 8px 14px 9px;
}

.chat--max .chat__log {
  gap: 10px;
}

.chat__talk {
  margin-left: auto;
}

.chat__log {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-right: 2px;
}

.chat__empty {
  line-height: 1.7;
}

.chat__turn {
  display: flex;
  font-size: 12px;
  line-height: 1.6;
}

/*
 * Bubbles, because a wall of left-aligned lines with a two-character label in front
 * of each is a log file, not a conversation. Which side is talking is carried by the
 * fill (the user's side gets the cyan wash), not by a squared-off corner — every
 * corner here is rounded, which is what the rest of the window is too.
 */
.chat__turn--user {
  justify-content: flex-end;
}

.chat__turn--assistant {
  justify-content: flex-start;
}

.chat__bubble {
  max-width: 88%;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 12px 9px;
  /*
   * 靠说话人那一侧的角收小：微信、iMessage、Ant Design X 的 Bubble 都是这个形状，
   * 眼睛靠它判断"这句是谁说的"，比那一行「小夜」两个字快。
   */
  border-radius: 4px 12px 12px;
  /* 表面取自皮肤：写死一档 rgba 会在换成琥珀/玫瑰的那套皮肤上留一个不属于任何皮肤的黑箱子。 */
  background: var(--hud-surface);
  border: 1px solid var(--hud-line);
}

.chat__turn--user .chat__bubble {
  border-radius: 12px 4px 12px;
  /* 皮肤动的是色相 token，这里写死 cyan 的话，换到琥珀/玫瑰那一套就是不属于任何皮肤的一块蓝。 */
  background: linear-gradient(
    180deg,
    color-mix(in srgb, var(--hud-cyan) 16%, transparent),
    color-mix(in srgb, var(--hud-cyan) 6%, transparent)
  );
  border-color: color-mix(in srgb, var(--hud-cyan) 34%, transparent);
}

/* The chairs. A wrap row of toggles, because the list is however many models exist
   and a dropdown that hides which ones are already sitting would defeat the point. */
.chat__seats {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  padding: 4px 0 6px;
}

.chat__seat {
  font-size: 11px;
  opacity: 0.72;
}

.chat__seat--on {
  opacity: 1;
  border-color: currentColor;
}

.chat__seat-rounds {
  font-size: 11px;
  margin-left: auto;
}

.chat__seat-hint {
  color: var(--hud-dim);
  font-size: 10px;
}

.chat__seat-cost {
  margin-left: 8px;
  color: var(--hud-cyan);
  font-size: 10px;
  letter-spacing: 0.3px;
  white-space: nowrap;
}

.chat__knob--on {
  opacity: 1;
  border-color: currentColor;
}

.chat__bubble--thinking {
  border-style: dashed;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

/*
 * The card that replaces an answer that never came. Red border and no animation: this is
 * a finished state that wants reading, not another thing moving on the screen.
 */
.chat__bubble--failed {
  border-color: rgba(255, 107, 107, 0.55);
}

.chat__failed-title {
  color: var(--hud-red);
  letter-spacing: 0.08em;
}

.chat__failed-reason {
  margin: 0;
  word-break: break-word;
}

.chat__failed-hint {
  margin: 0;
  color: var(--hud-cyan);
  font-size: 11px;
  line-height: 1.6;
}

.chat__failed-where {
  margin: 0;
  color: var(--hud-dim);
  font-size: 10px;
}

.chat__thinking-word {
  color: var(--hud-cyan);
  letter-spacing: 0.08em;
}

.chat__thinking-phase {
  color: var(--hud-dim);
  font-size: 11px;
}

/*
 * The two controls on the thinking bubble, pushed to its right edge. Small and quiet on
 * purpose: they are the only things here that act on a round that has not finished, so
 * they have to be findable without reading as part of the answer.
 */
.chat__thinking-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.chat__thinking-actions .hud-btn {
  padding: 2px 9px;
  font-size: 10px;
}

/* 排队中的那几枚，一句一枚：能看见叠了几句，也能一句一句撤掉。 */
.chat__queue {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.chat__queue-label {
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--hud-amber);
}

.chat__chip--queued {
  border-color: rgba(255, 181, 71, 0.34);
  background: rgba(255, 181, 71, 0.08);
}

.chat__queue-seq {
  color: var(--hud-dim);
  font-size: 9px;
}

.chat__who {
  color: var(--hud-dim);
  letter-spacing: 0.06em;
  font-size: 10px;
}

.chat__text {
  /* 1.65 + 13px：对话列是"读"的地方，行高压到 1.4 以下就是一堵墙。 */
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.chat__turn--assistant .chat__who {
  color: var(--hud-cyan);
}

.chat__turn--assistant .chat__text {
  color: var(--hud-text);
}

/*
 * The thinking chain is deliberately quieter than the answer under which it sits: same
 * font, dimmer, and wrapped in a <details> nobody has to open. Pre-wrap because the
 * model writes it in sentences and the operator reads it as prose.
 */
.chat__reasoning {
  margin-top: 6px;
  border-left: 2px solid var(--hud-line);
  padding-left: 8px;
}

.chat__reasoning > summary {
  cursor: pointer;
  color: var(--hud-dim);
  list-style: none;
}

.chat__reasoning > summary::-webkit-details-marker {
  display: none;
}

.chat__tools {
  /*
   * 原来这里挂着 opacity: .75 —— 叠在 --hud-dim 上把这句"本轮调用了哪条工具"压到
   * 约 4.0:1，正好掉在 AA 线下。它现在和「复制这段」并排，靠字号分层就够，不靠半透明。
   */
  font-size: 11px;
  color: var(--hud-dim);
}

.chat__reasoning-text {
  margin: 4px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.86em;
  color: var(--hud-dim);
  opacity: 0.85;
}

/*
 * 这一轮的产出。图片自己占一块，文件名那一行走同一套排版，两行都跟着一条按钮排。
 *
 * 颜色一律取自皮肤的那几枚变量：这一窗口的皮肤系统只动色相 token，写死一套色就会在
 * 换成琥珀/玫瑰的那一套皮肤上留下一个不属于任何皮肤的黑箱子。
 */
.chat__arts {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 10px;
  max-width: 100%;
}

.chat__art {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  min-width: 0;
}

.chat__art-pic {
  /*
   * 260 而不是 320：IM 会话里的缩略图普遍落在 240–280 这一档（融云的默认最长边就是
   * 240），再宽就把对话列顶成一屏，读起来不像对话。圆角跟 --hud-radius 分档：
   * 图 8、卡片 12，两档就够，多一档就开始像拼凑的。
   */
  max-width: 260px;
  width: 100%;
  border: 1px solid var(--hud-edge);
  border-radius: 8px;
  cursor: pointer;
}

.chat__art-wait {
  margin: 0;
  padding: 8px 10px;
  border: 1px dashed var(--hud-edge);
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--hud-dim);
  word-break: break-word;
}

/*
 * 一张卡片，不是一个文件名加一排按钮：左边角标、中间两行（名字 + 多大·什么时候）、
 * 右边只留一颗主操作和一颗 ⋯。名字截断，按钮永不换行 —— 用户说的"太丑"就是
 * 四颗按钮 wrap 成两行、把名字挤到上面去。
 */
.chat__art-file {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  max-width: 380px;
  padding: 8px 10px;
  border: 1px solid var(--hud-edge);
  border-radius: 12px;
  /* 层次靠"比背景亮一层的面"，不靠黑阴影投在近黑底上（那等于没有）。 */
  background: var(--hud-surface);
}

.chat__art-icon {
  flex: none;
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 7px;
  font-family: var(--hud-mono);
  font-size: 10px;
  letter-spacing: 0.02em;
  color: #04101a;
  background: var(--hud-cyan);
}

.chat__art-icon[data-ext='PDF'] {
  background: var(--hud-red);
}

.chat__art-icon[data-ext='DOCX'] {
  background: var(--hud-cyan);
}

.chat__art-icon[data-ext='XLSX'] {
  background: var(--hud-green);
}

.chat__art-icon[data-ext='PPTX'] {
  background: var(--hud-amber);
}

.chat__art-meta {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.chat__art-name {
  font-size: 12px;
  line-height: 1.4;
  color: var(--hud-text);
  /* 可选：拖得动名字才谈得上复制它，省略号只是这一格放不下时的样子。 */
  user-select: text;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat__art-sub {
  font-size: 11px;
  line-height: 1.4;
  color: var(--hud-dim);
}

.chat__art-acts {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 6px;
}

.chat__art-row {
  display: inline-flex;
  flex-wrap: nowrap;
  align-items: center;
  gap: 8px;
}

/* 12px 字号 + 26px 高（--hud-control-h）：10px 那一档在任何设计体系里都不存在。 */
.chat__art-btn {
  padding: 0 10px;
  font-size: 12px;
}

/* 溢出菜单那颗：视觉上 20px，命中区补到 26×26（WCAG 2.5.8 的最小 24）。 */
.chat__art-more {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: 1px solid var(--hud-line);
  border-radius: 7px;
  background: transparent;
  color: var(--hud-dim);
  font: inherit;
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}

.chat__art-more:hover {
  color: var(--hud-text);
  border-color: var(--hud-edge);
  background: rgba(77, 216, 255, 0.1);
}

.chat__art-why {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-amber);
  word-break: break-word;
}

/*
 * 气泡的页脚：复制这段 + "本轮调用了哪条工具"并成一行。
 * 原来那颗「复制」自己占一行、和产物那排按钮长成一个样，看上去像第四颗按钮。
 */
.chat__footline {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.chat__foot-btn {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--hud-dim);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.chat__foot-btn:hover {
  color: var(--hud-cyan);
  text-decoration: underline;
}

/* Three dots, staggered. Deliberately CSS-only: a JS-driven spinner would add one
   more timer to a window that is measured on how little it idles. */
/*
 * The 「思考中」 loader. One markup, four shapes, chosen in 设置 → 思考状态 Loader --
 * and the same choice is pushed to the desktop figure, which draws its own copy of this
 * wait in a window that has no CSS at all.
 */
.chat__loader {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 2px 3px;
}

.chat__loader i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--hud-cyan);
  animation: chat-think 1.05s infinite ease-in-out;
}

.chat__loader i:nth-child(2) {
  animation-delay: 0.15s;
}

.chat__loader i:nth-child(3) {
  animation-delay: 0.3s;
}

/* 只有前三个点是 dots 那种；其余形状自己决定用几格。 */
.chat__loader i:nth-child(n + 4) {
  display: none;
}

.chat__loader--matrix i {
  width: 4px;
  height: 9px;
  border-radius: 1px;
  animation: chat-matrix 1.15s infinite steps(6);
}

.chat__loader--matrix i:nth-child(2) {
  animation-delay: 0.2s;
}

.chat__loader--matrix i:nth-child(3) {
  animation-delay: 0.42s;
}

.chat__loader--matrix i:nth-child(n + 4) {
  display: block;
}

.chat__loader--ring {
  width: 14px;
  height: 14px;
  padding: 0;
}

.chat__loader--ring i {
  display: none;
  width: 13px;
  height: 13px;
  border: 2px solid transparent;
  border-top-color: var(--hud-cyan);
  border-right-color: var(--hud-cyan);
  border-radius: 50%;
  background: none;
  animation: chat-spin 0.85s infinite linear;
}

.chat__loader--ring i:first-child {
  display: block;
}

.chat__loader--bars {
  align-items: flex-end;
  height: 14px;
  gap: 3px;
}

.chat__loader--bars i {
  display: block;
  width: 3px;
  height: 14px;
  border-radius: 1px;
  animation: chat-bars 0.9s infinite ease-in-out;
}

.chat__loader--bars i:nth-child(2) {
  animation-delay: 0.12s;
}

.chat__loader--bars i:nth-child(3) {
  animation-delay: 0.24s;
}

.chat__loader--bars i:nth-child(4) {
  animation-delay: 0.36s;
}

@keyframes chat-matrix {
  0%,
  100% {
    opacity: 0.15;
    transform: scaleY(0.4);
  }
  40% {
    opacity: 1;
    transform: scaleY(1);
  }
}

@keyframes chat-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes chat-bars {
  0%,
  100% {
    transform: scaleY(0.35);
  }
  50% {
    transform: scaleY(1);
  }
}

@keyframes chat-think {
  0%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  50% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .chat__loader i {
    animation: none;
    opacity: 0.7;
  }
}

.chat__error {
  color: var(--hud-red);
  font-size: 12px;
}

/* 不是报错，所以不是红的；但也不能和正文一个颜色，那等于混进她说的话里。 */
.chat__note {
  margin: 0;
  color: var(--hud-dim);
  font-size: 11px;
}

.chat__form {
  display: flex;
  gap: 8px;
}

.chat__input {
  flex: 1;
  min-width: 0;
}

.chat__input:disabled {
  color: var(--hud-dim);
}

.chat__pending {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chat__chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 3px 10px;
  font-size: 10px;
  color: var(--hud-text);
  border: 1px solid rgba(77, 216, 255, 0.3);
  background: rgba(77, 216, 255, 0.07);
  border-radius: var(--hud-pill);
}

.chat__chip img {
  width: 18px;
  height: 18px;
  object-fit: cover;
  border-radius: var(--hud-radius);
}

.chat__chip-x {
  border: none;
  background: transparent;
  color: var(--hud-dim);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
}

.chat__chip-x:hover {
  color: var(--hud-red);
}

.chat__files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 4px;
}

.chat__thumb {
  max-width: 120px;
  max-height: 90px;
  border-radius: var(--hud-radius);
  border: 1px solid rgba(77, 216, 255, 0.3);
}

.chat__file {
  padding: 2px 10px;
  font-size: 10px;
  color: var(--hud-dim);
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-pill);
}

.chat__notice {
  margin: 0;
  font-size: 11px;
  color: var(--hud-amber);
}

/*
 * At the window's minimum width the middle column is about 260px, and a title plus
 * three buttons does not fit on one line. Letting the header wrap is the honest
 * answer; the alternative -- which is what happened -- is the buttons riding up over
 * the panel border and into the storage column.
 */
.chat__main > .hud-title {
  flex-wrap: wrap;
  row-gap: 6px;
  min-width: 0;
}

.chat {
  overflow: hidden;
}

.chat__log {
  min-height: 0;
  overflow-y: auto;
}
</style>
