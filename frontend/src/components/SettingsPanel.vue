<template>
  <div v-if="open" class="settings" role="dialog" aria-modal="true" aria-label="设置">
    <div class="settings__scrim" @click="emit('close')"></div>

    <section class="settings__box">
      <header class="settings__head">
        <h2 class="hud-title">设置 · SETTINGS</h2>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <p v-if="loadError" class="settings__error">{{ loadError }}</p>

      <div v-if="loading" class="settings__loading">读取中…</div>

      <form v-else-if="form" class="settings__body" @submit.prevent="save">
        <!--
          The scroller is a div inside the form rather than the form itself: 保存 is the
          footer's submit button, and it has to stay associated with this form *and*
          pinned while the groups under it scroll.
        -->
        <div class="settings__scroll">
        <fieldset class="settings__group">
          <legend>AI 模型</legend>

          <!--
            The list is the source of truth the chat dropdown reads: every row here
            becomes one entry there, whether it came from config.yaml or was added
            in this window. 「在用」 is the row the assistant answers with right now.
          -->
          <ul class="settings__models">
            <li
              v-for="row in models"
              :key="row.name"
              class="settings__model"
              :class="{ 'settings__model--on': row.current, 'settings__model--edit': row.name === editing }"
            >
              <button class="settings__model-pick" type="button" @click="editRow(row.name)">
                {{ row.name }}
              </button>
              <span class="settings__model-meta">
                {{ row.models.length }} 个模型 · 起始 {{ row.default_model }}
                · {{ row.source }}<template v-if="row.edited"> · 已改</template>
                · key {{ row.key_optional ? '不需要' : row.key_set ? '已设置' : '未设置' }}
              </span>
              <span class="settings__model-ops">
                <button
                  v-if="row.current"
                  class="hud-btn"
                  type="button"
                  title="对话区正在用这一行"
                  disabled
                >
                  在用
                </button>
                <button v-else class="hud-btn" type="button" :disabled="saving" @click="useRow(row.name)">
                  设为当前
                </button>
                <button
                  v-if="!row.current && row.source === '界面添加'"
                  class="hud-btn settings__del"
                  type="button"
                  :disabled="saving"
                  @click="removeRow(row.name)"
                >
                  删除
                </button>
                <button
                  v-else-if="!row.current"
                  class="hud-btn"
                  type="button"
                  :disabled="saving"
                  title="配置文件里那一行不动，只是不再出现在下拉框里"
                  @click="hideRow(row.name)"
                >
                  藏起来
                </button>
              </span>
            </li>
          </ul>

          <p class="settings__why">
            点名字编辑那一行的地址和模型名；「设为当前」立刻生效，不用重启。配置文件里的行删不掉、也改不回文件——界面上能改的是这一行怎么用，要真删请自己开 config.yaml。
          </p>

          <div v-if="hiddenRows.length" class="settings__hidden">
            <p class="hud-label">已藏起来 {{ hiddenRows.length }} 行（下拉框里看不到，配置没动）</p>
            <ul class="settings__models">
              <li v-for="row in hiddenRows" :key="`hidden-${row.name}`" class="settings__model">
                <span class="settings__model-pick">{{ row.name }}</span>
                <span class="settings__model-meta">
                  {{ row.models }} 个模型 · {{ row.source }}
                </span>
                <span class="settings__model-ops">
                  <button class="hud-btn" type="button" :disabled="saving" @click="showRow(row.name)">
                    找回
                  </button>
                </span>
              </li>
            </ul>
          </div>

          <label class="settings__row">
            <span class="hud-label">正在编辑：{{ editing }}</span>
          </label>

          <!--
            A provider's models, editable one row at a time. This is the list the chat
            header's second dropdown reads, so adding here and adding there are the same
            edit -- the panel is just somewhere with room to see the whole set.

            The last row cannot be removed: a provider with no models is one neither the
            picker nor the client factory can use, so the backend refuses it and this
            disables the button rather than letting the click fail.
          -->
          <div class="settings__mlist">
            <span class="hud-label">该服务商的模型 · {{ editingModels.length }}</span>
            <ul class="settings__mrow">
              <li v-for="spec in editingModels" :key="spec.id" class="settings__mitem">
                <span class="settings__mname">{{ spec.label }}</span>
                <span v-if="spec.id !== spec.label" class="settings__mid hud-label">{{ spec.id }}</span>
                <span v-if="spec.id === editingRow?.default_model" class="settings__mtag hud-label">起始</span>
                <!--
                  实测一次：问它一句能不能答，再递一张红方块问是什么颜色。
                  新加的模型不用按 —— 保存这条自己就会测，连不上就原样退回。这个按钮是给
                  config.yaml 里那些从来没被测过的老行补一次测量的，看图能力只有测过才知道。
                -->
                <button
                  class="hud-btn"
                  type="button"
                  :disabled="testing !== ''"
                  title="问它两句：能不能答、这张红图看不看得见"
                  @click="testModel(spec.id)"
                >
                  {{ testing === spec.id ? '测中…' : '测一下' }}
                </button>
                <small v-if="verdicts[`${editing}/${spec.id}`]" class="settings__why">{{ verdicts[`${editing}/${spec.id}`] }}</small>
                <button
                  v-if="!modelsLocked"
                  class="hud-btn settings__del"
                  type="button"
                  :disabled="saving || editingModels.length <= 1"
                  :title="editingModels.length <= 1 ? '至少要留一个模型' : `从 ${editing} 里删掉 ${spec.id}`"
                  @click="removeModel(spec.id)"
                >
                  删
                </button>
              </li>
            </ul>
            <div v-if="!modelsLocked" class="settings__madd">
              <input
                v-model.trim="newModel"
                class="settings__minput"
                type="text"
                spellcheck="false"
                placeholder="模型 id，如 deepseek-chat"
                @keydown.enter.prevent="addModel"
              />
              <input
                v-model.trim="newLabel"
                class="settings__minput"
                type="text"
                spellcheck="false"
                placeholder="显示名（可选）"
                @keydown.enter.prevent="addModel"
              />
              <button class="hud-btn" type="button" :disabled="saving || !newModel" @click="addModel">
                ＋ 加模型
              </button>
            </div>
            <!--
              自带模型那一行的模型表不是这一行决定的：它在下面「选哪个离线模型」那里选。
              后端对这一行的加/删直接回一句拒绝，所以这里把按钮拿掉而不是留一枚点了必然失败的。
            -->
            <p v-if="modelsLocked" class="settings__why">
              这一行用哪个离线模型，由下面「选哪个离线模型」那一格决定，这里不加也不删。
            </p>
            <p v-if="modelNote" class="settings__why">{{ modelNote }}</p>
          </div>

          <label class="settings__row">
            <span class="hud-label">调用地址</span>
            <input v-model.trim="form.base_url" type="text" spellcheck="false" :placeholder="editingRow?.base_url" />
          </label>
          <p class="settings__why">留空 = 不覆盖，用这一行本来的地址。</p>

          <label class="settings__row">
            <span class="hud-label">起始模型（该服务商默认用哪个）</span>
            <input
              v-model.trim="form.model"
              type="text"
              spellcheck="false"
              :disabled="modelsLocked"
              :placeholder="modelsLocked ? '由「选哪个离线模型」决定' : editingRow?.default_model"
            />
          </label>
          <p class="settings__why">
            <template v-if="modelsLocked">
              自带模型这一行跟着「选哪个离线模型」走：那里换成哪个，这里和实际发出去的就是哪个。
            </template>
            <template v-else>对话区第一次选到这个服务商时用它；留空 = 不改这一行（和调用地址同一个道理）。</template>
          </p>

          <div class="settings__row">
            <span class="hud-label">API Key（{{ editingRow?.key_env || '未配置' }}）</span>
            <div class="settings__keyline">
              <input
                v-model="apiKey"
                :type="revealKey ? 'text' : 'password'"
                autocomplete="off"
                spellcheck="false"
                :placeholder="keyPlaceholder"
              />
              <button class="hud-btn" type="button" @click="revealKey = !revealKey">
                {{ revealKey ? '隐藏' : '显示' }}
              </button>
              <button class="hud-btn" type="button" @click="apiKey = ' '">清除</button>
            </div>
          </div>
          <p class="settings__why">
            状态 {{ editingRow?.key_set ? '已设置' : '未设置' }}。密钥只写进环境变量（本进程立刻生效 + Windows 用户环境以便重启后仍在），不写进任何配置文件，也不会回显到页面上。
          </p>

          <label class="settings__row">
            <span class="hud-label">这一家的超时（秒）</span>
            <input
              v-model="rowTimeoutText"
              class="settings__minput"
              type="number"
              min="5"
              max="600"
              step="1"
              spellcheck="false"
              :placeholder="String(editingRow?.timeout_seconds ?? globalTimeout)"
            />
          </label>
          <p class="settings__why">
            留空 = 跟全局的 {{ globalTimeout }} 秒。本地大模型第一次问要把权重读进显存，
            撞超时的表现是"问一句转很久然后报错"；这一格调大只影响这一家，云端那几家不受牵连。
          </p>

          <label class="settings__row settings__row--inline">
            <input v-model="rowNoKey" type="checkbox" />
            <span class="hud-label">这个端点不需要 API Key（本地模型 / 内网服务）</span>
          </label>
          <p class="settings__why">
            现在这一行是「{{ editingRow?.key_optional ? '免 Key' : '要 Key' }}」。勾上就不发Authorization 头，本机 Ollama、LM Studio、llama.cpp 自带的 server 都是这样；环境里存着钥匙的话照样会用。不勾的话没钥匙会被客户端在发请求之前直接拒掉。
          </p>

          <details class="settings__add">
            <summary class="hud-label">
              ＋ 添加服务商（一次可以加几家）
              <b v-if="pendingProviders.length" class="hud-num settings__queue-count">
                待添加 {{ pendingProviders.length }}
              </b>
            </summary>
            <p class="settings__why">
              下面这四格说的是一家**新的**服务商（一个新地址、一把新钥匙）。已经在这张表里的服务商，点它的名字进去改地址、改起始模型、加它的其它模型。
            </p>
            <div class="settings__presets">
              <span class="hud-label">本地模型一键填</span>
              <button
                v-for="preset in LOCAL_PRESETS"
                :key="preset.url"
                class="hud-btn"
                type="button"
                :title="`${preset.url} —— 还要填你本机那个模型的名字`"
                @click="usePreset(preset)"
              >
                {{ preset.label }}
              </button>
            </div>
            <label class="settings__row">
              <span class="hud-label">名字（小写字母数字 - _）</span>
              <input v-model.trim="draft.name" type="text" spellcheck="false" placeholder="如 deepseek-v4" />
            </label>
            <label class="settings__row">
              <span class="hud-label">调用地址</span>
              <input v-model.trim="draft.base_url" type="text" spellcheck="false" placeholder="https://api.example.com/v1 或 http://localhost:11434/v1" />
            </label>
            <label class="settings__row">
              <span class="hud-label">模型名（可以加好几个，第一个就是起始模型）</span>
              <input
                v-model.trim="draft.model"
                type="text"
                spellcheck="false"
                placeholder="deepseek-chat，回车加进这一行"
                @keydown.enter.prevent="toggleDraftModel(draft.model)"
              />
            </label>
            <div v-if="draftedModels.length" class="settings__presets">
              <span class="hud-label">这一行要带的模型（{{ draftedModels.length }} 个，点一下去掉）</span>
              <button
                v-for="(id, index) in draftedModels"
                :key="id"
                class="hud-btn hud-btn--on"
                type="button"
                :title="`去掉 ${id}`"
                @click="toggleDraftModel(id)"
              >
                {{ index === 0 ? '起始 ' : '' }}{{ id }} ×
              </button>
            </div>
            <div class="settings__add-ops">
              <button
                class="hud-btn"
                type="button"
                :disabled="!/^https?:\/\//.test(draft.base_url) || listingModels"
                :title="
                  listingModels
                    ? '在问它要清单'
                    : '对它发一次 GET /models；这一格里的 Key 会一起发（只发这一次，不存不下日志）'
                "
                @click="fetchEndpointModels"
              >
                {{ listingModels ? '问它要清单…' : '看看它有哪些模型' }}
              </button>
              <span v-if="endpointModelsError" class="settings__why settings__why--bad">
                {{ endpointModelsError }}
              </span>
            </div>
            <div v-if="endpointModels.length" class="settings__presets">
              <span class="hud-label">它列出来的模型（点一下加进这一行，再点取消）</span>
              <button
                v-for="id in endpointModels"
                :key="id"
                class="hud-btn"
                :class="{ 'hud-btn--on': draftedModels.includes(id) }"
                type="button"
                @click="toggleDraftModel(id)"
              >
                {{ draftedModels.includes(id) ? '✓ ' : '' }}{{ id }}
              </button>
            </div>
            <label class="settings__row">
              <span class="hud-label">这家超时（秒，空 = 跟全局 {{ globalTimeout }}）</span>
              <input
                v-model.trim="draft.timeout_seconds"
                class="settings__minput"
                type="number"
                min="5"
                max="600"
                step="1"
                spellcheck="false"
              />
            </label>
            <p class="settings__why">
              一家端点常常有好几个模型，这里一次填完；加好之后也随时能在那一行里继续增删。
            </p>
            <label class="settings__row settings__row--inline">
              <input v-model="draft.key_optional" type="checkbox" />
              <span class="hud-label">这个端点不需要 API Key（本地模型 / 内网服务）</span>
            </label>
            <p v-if="draft.key_optional" class="settings__why">
              勾上就不发 Authorization 头。Ollama、LM Studio、llama.cpp 自带的 server 都是这样；不勾的话没钥匙会被客户端在发请求之前直接拒掉。
            </p>
            <label v-else class="settings__row">
              <span class="hud-label">API Key（存进 {{ draftKeyEnv }}）</span>
              <input v-model="draft.api_key" type="password" autocomplete="off" spellcheck="false" />
            </label>
            <div class="settings__add-ops">
              <button
                class="hud-btn"
                type="button"
                :disabled="!draftReady"
                :title="draftProblem || '填好名字、地址和起始模型就能加进来'"
                @click="queueProvider"
              >
                加进待添加
              </button>
              <span v-if="draftTouched && draftProblem" class="settings__why settings__why--bad">
                {{ draftProblem }}
              </span>
            </div>

            <ul v-if="pendingProviders.length" class="settings__queue">
              <li v-for="(row, index) in pendingProviders" :key="row.name">
                <b class="hud-num">{{ row.name }}</b>
                <span class="settings__queue-url">
                  {{ row.base_url }} · {{ row.models.join('、') || row.model }}
                </span>
                <span class="hud-label">
                  {{ row.key_optional ? '免 Key' : row.api_key ? '带 Key' : '' }}
                </span>
                <button class="hud-btn" type="button" @click="unqueueProvider(index)">去掉</button>
              </li>
            </ul>
            <p class="settings__why">
              <template v-if="pendingProviders.length">
                待添加 {{ pendingProviders.length }} 家，随「保存」一起生效；没点「加进待添加」的那一格也算进去。
              </template>
              <template v-else>添加随「保存」一起生效；名字不能和已有行重复。</template>
            </p>
          </details>
        </fieldset>

        <fieldset class="settings__group">
          <legend>语音</legend>
          <label class="settings__row settings__row--inline">
            <input v-model="form.auto_speak_typed" type="checkbox" />
            <span class="hud-label">打字问的问题也念出来{{ tag('auto_speak_typed') }}</span>
          </label>
          <p class="settings__why">关掉后只有语音问句会得到语音回答，文字回合只出字。</p>

          <div class="settings__row">
            <span class="hud-label">云端复刻 Key（{{ cloudVoiceKeyVariable }}）</span>
            <div class="settings__keyline">
              <input
                v-model="cloudVoiceKey"
                type="password"
                autocomplete="off"
                spellcheck="false"
                :placeholder="cloudVoiceKeySet ? '已设置，输入新值可替换' : '粘贴厂商给的 Key'"
              />
              <button class="hud-btn" type="button" :disabled="!cloudVoiceKey.trim()" @click="saveCloudVoiceKey">
                存这把
              </button>
            </div>
          </div>
          <p class="settings__why">
            状态 {{ cloudVoiceKeySet ? '已设置' : '未设置' }}。只有勾了「传到厂商做云端复刻」才会用到它；不勾就一次都不会发出去。和别家的钥匙一样只进环境变量。
          </p>

          <label class="settings__row">
            <span class="hud-label">思考状态 Loader</span>
            <select v-model="form.thinking_loader" class="hud-field">
              <option v-for="option in loaderOptions" :key="option.id" :value="option.id">
                {{ option.label }}
              </option>
            </select>
          </label>
          <p class="settings__why">
            「思考中」文案左边那种动效。对话气泡和桌面人物头上那张卡一起换 ——同一个等待不该长成两个样子。
          </p>


          <label class="settings__row">
            <span class="hud-label">她叫什么</span>
            <input
              v-model="form.display_name"
              type="text"
              :maxlength="nameMax"
              :placeholder="original?.display_name_default ?? ''"
            />
          </label>
          <p class="settings__why">
            左上角的品牌、窗口标题、托盘的悬停文字和菜单、还有她自我介绍，都跟着这个名字。留空就是用回默认的「{{ original?.display_name_default ?? '小夜' }}」。
            <b>不在这里改的东西</b>：可执行文件名、硬盘上的资产名，以及唤醒词 ——
            「你好小夜」那几个音是声学模型认下来的，改字不改声音，喊了不应；要换叫法去改唤醒词那格。窗口标题和托盘要下次启动才跟着变，左上角那一处立刻变。
          </p>

          <label class="settings__row">
            <span class="hud-label">唤醒问候语</span>
            <input
              v-model="form.wake_greeting"
              type="text"
              :maxlength="greetingMax"
              :placeholder="original?.wake_greeting_default ?? ''"
            />
          </label>
          <p class="settings__why">
            说「你好小夜」之后她先回的这一句。要等桌面人物完全显形才开口；宠物没开、或者主界面正开着的时候不等，直接说。清空就是不说。
          </p>

          <label v-if="wakeKeywordMax" class="settings__row">
            <span class="hud-label">唤醒词{{ tag('wake_keywords') }}</span>
            <input
              v-model="form.wake_keywords"
              type="text"
              spellcheck="false"
              :placeholder="wakeKeywordDefault"
            />
          </label>
          <p v-if="wakeKeywordMax" class="settings__why">
            说其中任何一个就算叫她。改完**下一句就生效**，不用重开麦克风 ——重启会把正在说的半句话吃掉。用顿号或空格分开，最多
            {{ wakeKeywordMax }} 个、每个 {{ wakeKeywordMinChars }}–{{ wakeKeywordMaxChars }} 个字：太短会在日常说话里撞到，太长是句子不是名字。
            <b>清空 = 交回配置里的那几个</b>（{{ wakeKeywordDefault }}），
            那几个是同音写法都认的版本，别改名把它们弄丢了。
          </p>

          <div class="settings__row">
            <span class="hud-label">音色{{ tag('tts_voice') }}</span>
            <div class="settings__keyline">
              <span class="settings__state">{{ voiceSummary }}</span>
              <button class="hud-btn" type="button" @click="emit('voice')">换音色</button>
            </div>
          </div>
          <p class="settings__why">
            {{ voiceNote }}
          </p>

          <div class="settings__row">
            <span class="hud-label">自带模型</span>
            <div class="settings__keyline">
              <span class="settings__state">{{ modelServerLine }}</span>
              <button
                class="hud-btn"
                type="button"
                :disabled="modelServerBusy || !modelServer.can_run || modelServer.phase !== 'off'"
                :title="
                  modelServer.phase === 'running'
                    ? '已经在跑了；要腾内存就点「停掉」'
                    : modelServer.phase === 'loading'
                      ? '正在把权重交给 llama.cpp'
                      : modelServer.can_run
                        ? '现在就加载，第一次要几十秒'
                        : modelServer.reason
                "
                @click="loadModelServer"
              >
                {{ modelServer.phase === 'loading' ? '加载中…' : '现在加载' }}
              </button>
              <button
                class="hud-btn"
                type="button"
                :disabled="modelServer.phase !== 'running'"
                @click="stopModelServer"
              >
                停掉
              </button>
            </div>
          </div>
          <p class="settings__why">
            {{ modelServerNote }}
          </p>
          <label class="settings__row">
            <span class="hud-label">选哪个离线模型</span>
            <select v-model="form.local_model" class="hud-field" @change="checkPick(form.local_model)">
              <option value="">自动（用包里最大的）</option>
              <option v-for="row in bundledModels" :key="row.file" :value="row.file" :disabled="!row.present">
                {{ modelLabel(row) }}{{ row.present ? '' : '（文件不在）' }}
              </option>
              <option value="none">不用离线模型（只走云端）</option>
            </select>
          </label>
          <p class="settings__why">
            {{ pickedModelNote }}
            <b>没选中的那几个不会被加载</b>，一个字节内存都不占；换的这一刻起要按上面的「停掉」再
            「现在加载」才换上新的。文件不在的灰着选不了（自己把 .gguf 放进「模型」目录就会出现）。
          </p>

          <label class="settings__row">
            <span class="hud-label">自带模型的上下文</span>
            <select v-model.number="form.local_context" class="hud-field">
              <option v-for="tier in contextTiers" :key="tier.id" :value="tier.id">
                {{ tier.label }} token
              </option>
            </select>
          </label>
          <p v-if="contextTierNote" class="settings__why">{{ contextTierNote }}</p>
          <p class="settings__why">这一档要约 {{ mbText(contextGate) }} 空闲内存才肯加载，这台机器现在剩 {{ mbText(localFreeMb) }}（不够就勾下面那项，或换云端模型）。<b>改完要按上面的「停掉」再「现在加载」才生效</b> —— 已经读进内存的那份不会自己变大。</p>

          <label class="settings__row">
            <span class="hud-label">问完多久把她关掉</span>
            <select v-model.number="form.local_idle_minutes" class="hud-field">
              <option v-for="row in idleOptions" :key="row.id" :value="row.id">
                {{ row.label }}
              </option>
            </select>
          </label>
          <p class="settings__why">
            闲够这么久她就把模型整个丢掉、把内存还给系统，下次问到再加载（本机实测 6–8 秒）。
            选「不自动关」= 一直占着约 5 GB，换来的是每一句都不用等加载 ——
            内存宽裕、又一直在聊，才值得这么选。
          </p>

          <label class="settings__row settings__row--inline">
            <input v-model="form.local_eager" type="checkbox" />
            <span class="hud-label">内存看着不够也照样加载</span>
          </label>
          <p class="settings__why">
            默认不勾。不勾时她先算一下空闲内存，不够就不加载、继续用云端模型，屏幕上写着为什么。勾上等于让程序别替你判断：内存真的够（比如 32 GB 的机器）又常被误拒时才勾；内存真不够的机器上，表现是整个电脑开始卡，不是她变慢。
          </p>

          <div class="settings__row">
            <span class="hud-label">一键按这台电脑配</span>
            <div class="settings__keyline">
              <span class="settings__state">{{ adviceBusy ? '她在看你的机器…' : '不用手填，先看一眼配置' }}</span>
              <button class="hud-btn" type="button" :disabled="adviceBusy" @click="askAdvice">
                给一套建议
              </button>
            </div>
          </div>
          <p class="settings__why">
            读一遍 CPU、内存、显卡和包里的模型，再问一次该用哪个、窗口开多大、闲多久关掉。问的是你当前在用的那个模型 —— 它要还没起来，这一按会先把她拉起来（十几秒）。
          </p>
          <div v-if="advice" class="settings__row">
            <span class="hud-label">
              建议（{{ advice.source === 'ai' ? '模型给的' : '本地规则算的' }}）
            </span>
            <p class="settings__why">
              {{ advice.reason }}
            </p>
            <p class="settings__why">
              模型：{{ advice.model === 'none' ? '不用离线模型' : advice.model }}；上下文 {{ advice.context }} token；问完闲 {{ advice.idle_minutes }} 分钟关掉；内存不够也硬上：{{ advice.eager ? '是' : '否' }}
            </p>
            <p v-if="advice.note" class="settings__why">{{ advice.note }}</p>
            <button class="hud-btn hud-btn--primary" type="button" :disabled="adviceBusy" @click="useAdvice">
              照这个配
            </button>
          </div>
          <p v-if="adviceError" class="settings__why settings__why--bad">{{ adviceError }}</p>


          <div class="settings__row">
            <span class="hud-label">桌面形象</span>
            <div class="settings__keyline">
              <span class="settings__state">{{ petSummary }}</span>
              <button class="hud-btn" type="button" @click="emit('pet')">生成或换</button>
            </div>
          </div>
          <p class="settings__why">
          给一张照片，本机描出轮廓做成有厚度的剪影，四个候选挑一个换上她。照片不保存、不联网；它没有骨骼，所以不会坐下或点头。
        </p>

        <div class="settings__row">
          <span class="hud-label">她占多大</span>
          <div class="settings__keyline">
            <button
              v-for="step in petScales"
              :key="step.id"
              class="hud-btn"
              :class="{ 'hud-btn--primary': step.id === petScale }"
              type="button"
              :disabled="petBusy"
              @click="chooseScale(step.id)"
            >
              {{ step.label }}
            </button>
            <button class="hud-btn" type="button" :disabled="petBusy" @click="flipIdle">
              {{ petIdle ? '她会自己动一动：开着' : '她会自己动一动：关着' }}
            </button>
          </div>
        </div>
        <p class="settings__why">
          <span>四档分别是 {{ petBoxLine }}。放大只是把她画得更大，多出来那一圈仍然是透明的。</span>
          <span>越大越费机器 —— 具体费多少按你这台算，这一句里不写别人机器上的数。</span>
          <span v-if="petNote">{{ petNote }}</span>
        </p>

          <div class="settings__row">
            <span class="hud-label">麦克风自动待命</span>
            <span class="settings__state">
              {{ original?.voice_auto_arm ? '开机会自己回到待唤醒' : '开机不自动开麦' }}
            </span>
          </div>
          <p class="settings__why">
            这一项由窗口右上角的「启用语音」/「释放麦克风」控制，不在这里改：开麦的同意必须来自那一次按压。
          </p>
        </fieldset>

        <fieldset class="settings__group">
          <legend>思考与上下文</legend>
          <!--
            这一行是 r85 加的：这三格以前读的是两个全局副本，而每次请求读的是**每一对
            模型自己那一行** —— 面板写着 2 轮、发出去是 10 轮（实测），勾掉思考照样带 4096
            token 的预算。现在两边是同一个值的两个入口，所以这里必须说清"改的是哪一对"。
          -->
          <p class="settings__why">
            这三格改的是<em>当前这一对模型</em>：{{ original?.tuning_pair || '—' }}
            <template v-if="original?.tuning_pinned">（这一对单独设过）</template>
            <template v-else>（还没单独设过，显示的是默认值）</template>。
            对话区顶部那几格是<em>同一份值</em> —— 在哪边改都行，存完立刻生效；换成别的模型各记各的。
          </p>
          <label class="settings__row settings__row--inline">
            <input v-model="form.thinking_enabled" type="checkbox" />
            <span class="hud-label">要她给出思考过程{{ tag('thinking_enabled') }}</span>
          </label>
          <p class="settings__why">
            开了之后每条回答下面会出现可展开的「思考过程」。这是向模型额外要的 token，不要就关掉；没开的模型不回这段，框也就是空的。
            <template v-if="!budgetHonored">
              自带模型这一家<em>实测能省十分之九的时间</em>：同一句「检查本机 JDK 版本」，
              开思考 32.2 秒、关思考 3.4 秒，工具照样调得成（r90 在 1.7B 和 4B 上各跑一遍）。
            </template>
          </p>

          <label class="settings__row">
            <span class="hud-label">思考预算（token）{{ tag('thinking_budget') }}</span>
            <input
              v-model.number="form.thinking_budget"
              type="number"
              :min="budgetBounds[0]"
              :max="budgetBounds[1]"
              step="64"
            />
          </label>
          <p class="settings__why">
            {{ budgetBounds[0] }}–{{ budgetBounds[1] }} 之间。这也是对话区那枚下拉背后的数字
            （不思考 0 / 轻度 1024 / 中度 4096 / 深度 16000），你自己填的数就按你自己填的发。
            <template v-if="budgetHonored">
              到数了她就停止思考直接答，回答里的思考 token 数能和这个值对上，所以能验证它没变成摆设。
            </template>
            <template v-else>
              <em>但这一格对自带模型没用</em>：随包那个 llama.cpp 不认这个字段，实测填
              32 / 128 / 512 / 2048 想出来的字数都一样（~850）。它这里只有"关得掉"，
              没有"调深浅" —— 中度、深度在自带模型上是同一件事。这个数会照常存着，
              换回云端那几家就生效。
            </template>
          </p>

          <label class="settings__row">
            <span class="hud-label">上下文轮数{{ tag('history_turns') }}</span>
            <input
              v-model.number="form.history_turns"
              type="number"
              :min="turnBounds[0]"
              :max="turnBounds[1]"
              step="1"
            />
          </label>
          <p class="settings__why">
            <b>这是轮数，不是上下文窗口</b>，两件事别混：轮数决定每次提问<em>带上几段历史</em>，
            上下文窗口是那一家模型<em>一次最多能吃多少 token</em> —— 历史带得再少，窗口装不下也答不上。
            可填 {{ turnBounds[0] }}–{{ turnBounds[1] }}，0 表示只看这一句；调大能接得上「接着刚才那个说」，但每一轮都要把这段重付一遍。云端那几家的窗口是厂商定的
            （超了会自动精简历史），自带模型的窗口在上面「自带模型的上下文」里自己调。
          </p>
        </fieldset>

        <fieldset v-if="alertRules.length" class="settings__group">
          <legend>告警</legend>
          <p class="settings__why">
            每一行是一条线：连续超过 {{ alertSustain }} 秒才算一次，恢复了才会关掉。关掉某一行只是不再盯它，不影响概览里的其它读数。
          </p>
          <div class="settings__row">
            <button class="hud-btn" type="button" @click="emit('alerts')">看现在开着的告警</button>
            <span class="hud-label">哪几条、现在多少、线在哪，都在弹窗里；「知道了」也在那。</span>
          </div>
          <div v-for="rule in alertRules" :key="rule.code" class="settings__rule">
            <label class="settings__row settings__row--inline">
              <input v-model="form.alert_rules[rule.code].enabled" type="checkbox" />
              <span class="hud-label">{{ rule.label }}{{ tag(`alerts_rules:${rule.code}`) }}</span>
            </label>
            <label class="settings__row settings__row--inline">
              <span class="hud-label">{{ rule.direction === 'above' ? '高于' : '低于' }}</span>
              <input
                v-model.number="form.alert_rules[rule.code].threshold"
                type="number"
                :min="rule.low"
                :max="rule.high"
                :step="rule.unit === 'GB' ? 1 : 0.5"
                class="settings__small"
              />
              <span class="hud-label">{{ rule.unit }}（可调 {{ rule.low }}–{{ rule.high }}）</span>
            </label>
            <p class="settings__why">{{ rule.help }}</p>
          </div>

          <label class="settings__row">
            <span class="hud-label">重复提醒间隔（分钟）{{ tag('alerts_cooldown_minutes') }}</span>
            <input
              v-model.number="form.alert_cooldown"
              type="number"
              :min="cooldownBounds[0]"
              :max="cooldownBounds[1]"
              step="1"
            />
          </label>
          <p class="settings__why">
            同一条告警在这一段时间里只说一次。设得太短，一块快满的盘会一直打断你。
          </p>

          <label class="settings__row settings__row--inline">
            <input v-model="form.alert_speak" type="checkbox" />
            <span class="hud-label">严重告警用语音提醒{{ tag('alerts_speak_critical') }}</span>
          </label>
          <p class="settings__why">
            只有「严重」这一档会开口，普通告警只在概览里亮着。语音没启用时这条本来就不会响。
          </p>
        </fieldset>

        <fieldset class="settings__group">
          <legend>界面</legend>
          <label class="settings__row">
            <span class="hud-label">遥测轮询间隔（毫秒）{{ tag('telemetry_interval_ms') }}</span>
            <input v-model.number="form.telemetry_interval_ms" type="number" min="500" max="60000" step="100" />
          </label>
          <p class="settings__why">
            500–60000 之间，保存后下一次轮询就用新间隔。调太小会让界面自己变成机器上最忙的进程。
          </p>
        </fieldset>

        <p v-if="savedNote" class="settings__saved">{{ savedNote }}</p>
        <p v-if="saveWarning" class="settings__why settings__why--bad">{{ saveWarning }}</p>
        <ul v-if="problemList.length" class="settings__problems">
          <li v-for="line in problemList" :key="line">{{ line }}</li>
        </ul>
        </div>

        <footer class="settings__foot">
          <span v-if="original?.overrides_active?.length" class="hud-label">
            生效中：{{ original.overrides_active.join('、') }}
          </span>
          <button class="hud-btn hud-btn--primary" type="submit" :disabled="saving">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </footer>
      </form>
    </section>

    <div v-if="gate" class="gate">
      <div class="gate__card" role="alertdialog" aria-modal="true" aria-label="这台机器接不住它">
        <h4 class="gate__title">{{ gate.model_label }}：这台机器接不住</h4>
        <p class="gate__why">{{ gate.reason }}</p>
        <p class="gate__nums">
          总内存 {{ gb(gate.total_mb) }} · 现在空闲 {{ gb(gate.free_mb) }} · 这一档要约
          {{ gb(gate.need_mb) }} · 文件本身 {{ gb(gate.file_mb) }} ·
          <template v-if="gate.vram_mb">显卡 {{ gate.gpu }}，显存 {{ gb(gate.vram_mb) }}</template>
          <template v-else>没查到独立显卡（会全走 CPU，慢一个数量级）</template>
          · 磁盘还剩 {{ gb(gate.disk_free_mb) }}
        </p>
        <p v-if="gate.hard" class="gate__hard">
          这一条没有「还是要试」：这么大的权重进不了这么大的内存，硬加载的表现是整个电脑卡住，不是她变慢。
        </p>
        <div class="gate__row">
          <button class="hud-btn hud-btn--primary" type="button" @click="gateUndo">换回上一个</button>
          <button v-if="!gate.hard" class="hud-btn" type="button" @click="gateForce">
            还是选它（勾上「照样加载」）
          </button>
          <button class="hud-btn" type="button" @click="gate = null">知道了</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * The settings panel: a list of models, and the switches around them.
 *
 * Every save goes through one bridge call and takes effect in the running process:
 * the LLM service drops its client cache when the effective section changes, the
 * key lands in the environment the client reads per request, and the poll interval
 * is handed to the telemetry store before this dialog even closes. "Restart to
 * apply" is the failure mode this panel is designed against.
 */
import { computed, ref, watch } from 'vue'

import {
  addProviderModel,
  fetchSettings,
  fetchVoices,
  listEndpointModels,
  llmTest,
  localAdvice,
  localAdviceApply,
  localModelCheck,
  modelServerStart,
  modelServerStatus,
  modelServerStop,
  petAssets,
  fetchPetState,
  setPetIdle,
  setPetScale,
  removeProviderModel,
  saveSettings,
  type AlertRuleSetting,
  type BundledModel,
  type HiddenRow,
  type GateCheck,
  type LocalAdvice,
  type ModelRow,
  type ModelServerStatus,
  type ModelSpec,
  type SettingsSnapshot,
  type VoiceList,
} from '@/api/bridge'

const props = defineProps<{ open: boolean; voiceClosed?: number; petClosed?: number }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'saved', snapshot: SettingsSnapshot): void
  (e: 'voice'): void
  (e: 'pet'): void
  (e: 'alerts'): void
}>()

const original = ref<SettingsSnapshot | null>(null)
const form = ref<{
  base_url: string
  model: string
  auto_speak_typed: boolean
  wake_greeting: string
  wake_keywords: string
  thinking_loader: string
  display_name: string
  local_model: string
  local_context: number
  local_idle_minutes: number
  local_eager: boolean
  telemetry_interval_ms: number
  thinking_enabled: boolean
  thinking_budget: number
  history_turns: number
  alert_rules: Record<string, { enabled: boolean; threshold: number }>
  alert_cooldown: number
  alert_speak: boolean
} | null>(null)
const editing = ref('')
const apiKey = ref('')
const revealKey = ref(false)
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const problems = ref<Record<string, string>>({})
const savedNote = ref('')
/** One row of the add-provider block. The shape is the patch's shape, so a queued row
 *  needs no translating on the way to the shell. */
interface ProviderDraft {
  name: string
  base_url: string
  /** The box being typed in. What gets stored is :attr:`models`; this folds into it on
   *  the way out, so a half-finished form never loses the last name somebody typed. */
  model: string
  /** Every model this new row carries. The first one is the 起始模型 -- one endpoint
   *  usually serves a family, and the 看看它有哪些模型 button has just listed them all. */
  models: string[]
  api_key: string
  key_optional: boolean
  /** Blank string = follow the section-wide timeout; the shell parses it. */
  timeout_seconds: string
}

/**
 * The three local servers people actually run, by their default port.
 *
 * Only the address and the no-key flag are filled in: the model name is whatever the
 * operator happened to `pull` or load, and guessing one would produce a row that looks
 * configured and answers "model not found" on the first question.
 */
/** 180s for the local three: a 7B model loading weights off a spinning disk blows
 *  straight through the section-wide 60s on the very first question, and a save that
 *  rolls the row back because of that reads as "your local model is broken". */
const LOCAL_PRESETS = [
  { label: 'Ollama', name: 'ollama', url: 'http://localhost:11434/v1', timeout: '180' },
  { label: 'LM Studio', name: 'lmstudio', url: 'http://localhost:1234/v1', timeout: '180' },
  { label: 'llama.cpp', name: 'llama-cpp', url: 'http://localhost:8080/v1', timeout: '180' },
] as const

const BLANK_PROVIDER_DRAFT: ProviderDraft = {
  name: '',
  base_url: '',
  model: '',
  models: [],
  api_key: '',
  key_optional: false,
  timeout_seconds: '',
}

const draft = ref<ProviderDraft>({ ...BLANK_PROVIDER_DRAFT })
/** Providers typed into the add block but not yet saved. The queue exists because the
 *  alternative was six clicks per provider -- fill, save, reopen the dialog. They go out
 *  with the save in **one** write, so a refusal covers all of them at once instead of
 *  leaving two stored and one lost with a single 「已保存」 over the lot. */
const pendingProviders = ref<ProviderDraft[]>([])

/**
 * One add-row on the way out: the list, with whatever is still in the box folded into it.
 *
 * Folding rather than refusing, because the box is where the last name always is: a form
 * that dropped it would lose the one model the operator typed by hand, and one that
 * demanded a separate 加进来 click for it would make the single-model case -- still the
 * common one -- cost two clicks. Duplicates collapse, order is kept: the first is 起始.
 */
function providerPayload(row: ProviderDraft): ProviderDraft {
  const seen = new Set<string>()
  const list: string[] = []
  for (const id of [...row.models, row.model]) {
    const wanted = id.trim()
    if (wanted && !seen.has(wanted)) {
      seen.add(wanted)
      list.push(wanted)
    }
  }
  return { ...row, model: list[0] ?? '', models: list }
}

/** What this row will carry, box included -- what the chips and the checks read. */
const draftedModels = computed<string[]>(() => providerPayload(draft.value).models)

/** The three rules the save enforces, checked here so 「加进待添加」 is not a button that
 *  looks dead. Names are matched against the rows on screen *and* against the queue:
 *  two pending rows with one name come back as one row, silently. */
const draftProblem = computed(() => {
  const row = draft.value
  if (!row.name) return '先填名字'
  if (!/^[a-z0-9_-]{1,32}$/.test(row.name)) return '名字只能是小写字母、数字、- 和 _，1-32 位'
  if (!/^https?:\/\//.test(row.base_url)) return '地址必须以 http:// 或 https:// 开头'
  if (!draftedModels.value.length) return '还要填至少一个模型'
  if (draftedModels.value.some((id) => id.includes(' ') || id.length > 150))
    return '模型名不能含空格且不超过 150 字符'
  if (!row.key_optional && !row.api_key.trim())
    return '没填 API Key：本地模型请勾「这个端点不需要 API Key」，别的服务商要填'
  if (row.timeout_seconds) {
    const seconds = Number(row.timeout_seconds)
    if (!Number.isFinite(seconds) || seconds < 5 || seconds > 600)
      return '超时秒数要在 5-600 之间，或者留空跟全局'
  }
  if (models.value.some((m) => m.name === row.name)) return `已经有叫 ${row.name} 的行，点它的名字进去改`
  if (pendingProviders.value.some((m) => m.name === row.name)) return `${row.name} 已经在待添加里了`
  return ''
})
const draftReady = computed(() => draftProblem.value === '')
/** An untouched form should not open with a red complaint. */
const draftTouched = computed(() =>
  Boolean(draft.value.name || draft.value.base_url || draftedModels.value.length),
)

/** Add one model to this row; clicking a name that is already in takes it back out. */
function toggleDraftModel(id: string): void {
  const wanted = id.trim()
  if (!wanted) return
  const list = draftedModels.value
  const rest = list.filter((item) => item !== wanted)
  draft.value = {
    ...draft.value,
    models: list.includes(wanted) ? rest : [...list, wanted],
    // The box is only a scratch field: whatever it held is now in the list.
    model: '',
  }
}

function queueProvider(): void {
  if (!draftReady.value) return
  pendingProviders.value.push(providerPayload(draft.value))
  draft.value = { ...BLANK_PROVIDER_DRAFT }
}

function unqueueProvider(index: number): void {
  pendingProviders.value.splice(index, 1)
}

/** Fills what is the same for everybody running that server, and leaves the model name
 *  alone because that one is genuinely theirs to type. */
function usePreset(preset: (typeof LOCAL_PRESETS)[number]): void {
  draft.value = {
    ...draft.value,
    name: draft.value.name || preset.name,
    base_url: preset.url,
    api_key: '',
    key_optional: true,
    timeout_seconds: preset.timeout,
  }
}
const newModel = ref('')
const newLabel = ref('')
const modelNote = ref('')
const voices = ref<VoiceList | null>(null)
const voiceError = ref('')

const models = computed<ModelRow[]>(() => original.value?.models ?? [])
/** Rows this window hides. Their rows are absent from ``models`` by design, so the list of
 *  what is *not* shown has to come from the shell rather than be inferred from the menu. */
const hiddenRows = computed<HiddenRow[]>(() => original.value?.hidden_models ?? [])
/** The range the panel advertises is the range the save enforces, read from the same
 * snapshot -- a hardcoded min/max here is a second copy that will drift. */
/** The same ceiling the save enforces, read from the snapshot rather than repeated here. */
const greetingMax = computed(() => original.value?.wake_greeting_max ?? 200)
/** The wake-word bounds and the shipped words, read from the shell -- same rule as above:
 *  a second copy of a limit here is a second limit that can disagree with the real one. */
const wakeKeywordMax = computed(() => original.value?.wake_keywords_max ?? 0)
const wakeKeywordMinChars = computed(() => original.value?.wake_keyword_min_chars ?? 2)
const wakeKeywordMaxChars = computed(() => original.value?.wake_keyword_max_chars ?? 12)
const wakeKeywordDefault = computed(() =>
  (original.value?.wake_keywords_default ?? []).join('、'),
)

/** The kinds the shell will accept, read from the snapshot -- same rule as the ranges. */
const loaderChoices = computed<string[]>(
  () => original.value?.thinking_loader_choices ?? ['dots', 'matrix', 'ring', 'bars'],
)

const loaderLabels: Record<string, string> = {
  dots: '三个点（默认）',
  matrix: '矩阵雨',
  ring: '圆环',
  bars: '竖条',
}

const budgetBounds = computed<[number, number]>(() => {
  const raw = original.value?.thinking_budget_bounds ?? []
  return raw.length === 2 ? [raw[0], raw[1]] : [64, 16000]
})
/** Whether the endpoint in view actually honours a thinking budget.

  False for the bundled llama.cpp: measured r90, 32/128/512/2048 all thought ~850
  characters while a `max_tokens` control on the same prompt scaled 93/382/853. The
  field is left editable because the number is stored per model and cloud picks it
  up -- but the row has to say what it does and does not do here. */
const budgetHonored = computed<boolean>(() => original.value?.thinking_budget_honored !== false)
const turnBounds = computed<[number, number]>(() => {
  const raw = original.value?.history_turns_bounds ?? []
  return raw.length === 2 ? [raw[0], raw[1]] : [0, 50]
})
/** The shipped alert lines, read from the engine that owns them -- a second list here
 *  would be a menu of rules the shell may not even have. */
const alertRules = computed<AlertRuleSetting[]>(() => original.value?.alerts?.rules ?? [])
/**
 * Which voice she speaks with, in the engine's own words.
 *
 * Read from ``tts_voices`` rather than kept in the settings snapshot: the picker is a
 * second reader of the same list, and a copy here would be a second copy that can go
 * stale the moment a voice is picked in the dialog this row opens.
 */
const voiceSummary = computed(() => {
  if (voiceError.value) return voiceError.value
  const list = voices.value
  if (!list) return '读取中…'
  if (list.error) return list.error
  const chosen = list.choices.find((row) => row.id === list.current)
  if (chosen) return `${chosen.label} · ${list.engine} · 共 ${list.choices.length} 个`
  return list.current || '没选过，用引擎默认的那个'
})
const voiceNote = computed(() =>
  voiceError.value || voices.value?.error
    ? '读不到就不装能改：这一项要等语音那侧答得出来才动得了。'
    : '「换音色」开的是对话区那同一个弹窗 —— 能试听，选完下一句就用它讲。',
)
/** What stands on the desktop right now, read from the same list the picker shows. */
/* The bundled llama.cpp server. Read on open, never started by reading -- a settings
   panel that allocates 5 GB because it was painted would be the worst kind of surprise. */
const modelServer = ref<ModelServerStatus>({
  phase: 'off',
  detail: '',
  can_run: false,
  reason: '还没问过程序',
  model: '',
  model_label: '',
  model_selected: '',
  models: [],
  port: 0,
  context: 4096,
  dialed_context: 4096,
  acts: false,
  needed_mb: 5200,
  eager: false,
  idle_seconds: 600,
  load_ms: 0,
  free_mb: 0,
})
const modelServerBusy = ref(false)

const modelServerLine = computed(() => {
  const state = modelServer.value
  if (state.phase === 'running') {
    return `在跑，端口 ${state.port}（起来用了 ${Math.round(state.load_ms / 1000)} 秒）`
  }
  if (state.phase === 'loading') return '正在加载权重…'
  if (state.phase === 'failed') return `起不来：${state.detail || state.reason}`
  if (!state.can_run) return state.reason
  return '没在跑（问到她的第一条会自己加载）'
})

/** What this tier gives and costs, said before it is chosen: no key and no upload, and
 * whether she can act depends on the window -- the tool table is a measured 4702 tokens. */
const modelServerNote = computed(() => {
  const state = modelServer.value
  if (!state.can_run) return state.reason + '。想离线用就把权重放进「模型」文件夹。'
  const reach = state.acts
    ? `上下文 ${state.context} 装得下那份工具清单，她能一边答一边动手。`
    : `上下文 ${state.context} 装不下那份工具清单，这一档她只回答、不动手；要她动手就把下面那档开大。`
  const idle =
    state.phase === 'running'
      ? ` 空闲 ${Math.round(state.idle_seconds / 60)} 分钟会自动关掉还内存。`
      : ''
  const stale =
    state.phase === 'running' && state.dialed_context !== state.context
      ? ` 上面选的 ${state.dialed_context} 要「停掉」再「现在加载」才换上，正在跑的这份不会自己变大。`
      : ''
  return `随包的 ${state.model_label}（离线、不要 Key、不上传）。${reach}${idle}${stale}`
})

/** The tiers and idle waits the shell offers; empty until the snapshot lands. */
const contextTiers = computed(() => original.value?.local_context_options ?? [])
const idleOptions = computed(() => original.value?.local_idle_options ?? [])
const nameMax = computed(() => original.value?.display_name_max ?? 12)
const localNeedsMb = computed(() => original.value?.local_needs_free_mb ?? 5200)
const localFreeMb = computed(() => original.value?.local_free_mb ?? 0)

/** The note for the tier on the dial -- measured resident, and whether the tools fit. */
const contextTierNote = computed(() => {
  const current = form.value
  if (!current) return ''
  const picked = contextTiers.value.find((tier) => tier.id === current.local_context)
  return picked?.note ?? `这一档（${current.local_context} token）没量过，程序不替你猜它要多少内存。`
})

/** The gate belonging to the tier on the dial, not the one last saved. */
const contextGate = computed(() => {
  const current = form.value
  const picked = current
    ? contextTiers.value.find((tier) => tier.id === current.local_context)
    : undefined
  return picked?.needs_free_mb ?? localNeedsMb.value
})

/** MB into 「5.1 GB」: the screen must not make the operator divide by a thousand. */
function mbText(mb: number): string {
  return `${(mb / 1024).toFixed(1)} GB`
}

/** 包里的离线模型清单。**和下拉绑的那个值取自同一次读设置**（`local_models`）——
 * 以前它读的是另一路轮询（`model_server_status().models`），两路一旦不同步，就会出现
 * "值说 A、表里那一行写的是 B"：真机上见过跑 0.6B 而这一格显示 Qwen2.5-7B。
 * 设置那份还没到手时（首帧）退回状态里那一份，不至于空着。 */
const bundledModels = computed<BundledModel[]>(
  () => original.value?.local_models ?? modelServer.value.models ?? [],
)

/** 下拉里的写法：名字 + 多大 + 要约几 GB 空闲。选模型就是在选内存，别只给一个名字。
 *  最后那一截是判词：0.6B 装得下、跑得快，可它不能聊天，挑的时候就得看见。 */
function modelLabel(row: BundledModel): string {
  const cost = row.measured ? `要约 ${mbText(row.gate_mb)} 空闲内存` : `按大小估约 ${mbText(row.gate_mb)} 空闲内存`
  const smoke = row.smoke_only ? '｜只够冒烟测试' : ''
  return `${row.label}｜${(row.size_mb / 1024).toFixed(1)} GB｜${cost}${smoke}`
}

/** 选中那一格的解释：装不装得下工具、是不是实测过、文件在不在。 */
const pickedModelNote = computed(() => {
  const current = form.value
  if (!current) return ''
  if (current.local_model === 'none') {
    return '现在选的是「不用离线模型」：她只走云端（设置里得配一个 Key），包里的权重一个字节都不进内存。'
  }
  const picked = bundledModels.value.find((row) => row.file === current.local_model)
  if (!picked) {
    return '「自动」= 用包里最大的那一个。想固定就在这里点名，换的一刻起只有选中的会被加载。'
  }
  const acts = picked.acts_verified
    ? '这台机器上实测见过它带 34 个工具调成一次'
    : '还没实测过它带工具能不能调成（装得下 ≠ 调成过）'
  const think = picked.thinking ? '；它默认爱先思考，把「思考」关掉回答才不会看着是空的' : ''
  const smoke = picked.smoke_only
    ? '；但这一档太小，实测会把新提问逐字答成上一轮那句原话，只够验证能不能连通，别拿它聊天'
    : ''
  return (
    `现在选的是 ${picked.label}：${(picked.size_mb / 1024).toFixed(1)} GB，` +
    `本机常驻约 ${mbText(picked.resident_mb)}（没显卡的机器约 ${mbText(picked.cpu_mb)}）。${acts}${smoke}${think}。`
  )
})

/** 门槛预检的结果。不为 null 就是弹窗正拦着，加载那条路必须先经过它。 */
const gate = ref<GateCheck | null>(null)
const gateWas = ref('')
/** 上一次「过了门槛」的那个选择：弹窗里的「换回上一个」回到它。
 * 不能到 @change 里现读 form.local_model —— v-model 已经先把新值写进去了。 */
const lastPassed = ref('')

/** 选中那一刻先问一句"这台接得住吗"。判定读的是后端 `verdict_for`，和真加载时拦的是同一段。 */
async function checkPick(model: string): Promise<void> {
  if (!model) return
  const current = form.value
  gateWas.value = lastPassed.value
  try {
    const verdict = await localModelCheck(model, current?.local_context ?? 4096)
    if (!verdict.ok) gate.value = verdict
    else lastPassed.value = model
  } catch (caught) {
    // 问不到预检不等于能加载：让后端在真正要起的那一刻自己拒。
    console.warn('门槛预检没问到', caught)
  }
}

function gateUndo(): void {
  if (form.value) form.value.local_model = gateWas.value
  gate.value = null
}

/** "还是选它"只在这条不是硬底线时存在：勾上照样加载，把决定权交回给人。 */
function gateForce(): void {
  if (form.value) {
    form.value.local_eager = true
    // 人自己点的这一条就算过了：下一次拦下来要回到它，不是回到更早的那个。
    lastPassed.value = form.value.local_model
  }
  gate.value = null
}

function gb(mb: number): string {
  return `${(mb / 1024).toFixed(1)} GB`
}

const advice = ref<LocalAdvice | null>(null)
const adviceBusy = ref(false)
const adviceError = ref('')

/** 问一次"这台机器该怎么配"。可能要等模型先起来，所以按钮自己变灰。 */
async function askAdvice(): Promise<void> {
  if (adviceBusy.value) return
  adviceBusy.value = true
  adviceError.value = ''
  try {
    advice.value = await localAdvice()
  } catch (caught) {
    adviceError.value = '问不到建议：' + String(caught)
  } finally {
    adviceBusy.value = false
  }
}

/** 「照这个配」：值原样送回后端，由后端再校验一遍（不在档位里的一律拒）。 */
async function useAdvice(): Promise<void> {
  const card = advice.value
  if (!card || adviceBusy.value) return
  adviceBusy.value = true
  adviceError.value = ''
  try {
    const result = await localAdviceApply(card)
    if (result.error) {
      adviceError.value = result.error
      return
    }
    // 后端返回的就是新快照：整块重读一遍，别只改屏幕上那几行。
    // 这里**只读状态**：loadModelServer 是「现在加载」那颗按钮的 handler，会真起一个
    // 几 GB 的子进程 —— 建议卡刚把旋钮写好就偷跑加载，和面板自己写的
    // 「换模型要先停掉再现在加载」直接矛盾（r86 审计）。
    await load()
    await readModelServer()
    advice.value = null
  } catch (caught) {
    adviceError.value = '照这个配没成功：' + String(caught)
  } finally {
    adviceBusy.value = false
  }
}

async function readModelServer(): Promise<void> {
  try {
    modelServer.value = await modelServerStatus()
  } catch (caught) {
    modelServer.value = { ...modelServer.value, can_run: false, reason: '问不到状态：' + String(caught) }
  }
}

async function loadModelServer(): Promise<void> {
  const current = form.value
  if (current?.local_model) {
    // 拦在点下去之前：这一按本来要等几十秒加载，失败了才在屏幕上写一行小字，太晚。
    try {
      const verdict = await localModelCheck(current.local_model, current.local_context)
      if (!verdict.ok) {
        gateWas.value = current.local_model
        gate.value = verdict
        return
      }
    } catch (caught) {
      console.warn('门槛预检没问到，交给后端判', caught)
    }
  }
  modelServerBusy.value = true
  modelServer.value = { ...modelServer.value, phase: 'loading', detail: '' }
  try {
    modelServer.value = await modelServerStart()
  } catch (caught) {
    modelServer.value = { ...modelServer.value, phase: 'failed', detail: String(caught) }
  } finally {
    modelServerBusy.value = false
  }
}

async function stopModelServer(): Promise<void> {
  modelServerBusy.value = true
  try {
    modelServer.value = await modelServerStop()
  } finally {
    modelServerBusy.value = false
  }
}

const petSummary = ref('读一下才知道')
const petScales = ref<{ id: string; label: string; multiplier: number }[]>([
  { id: 'small', label: '小', multiplier: 0.75 },
  { id: 'normal', label: '中', multiplier: 1 },
  { id: 'large', label: '大', multiplier: 1.35 },
  { id: 'giant', label: '特大', multiplier: 1.7 },
])
const petScale = ref('normal')
const petIdle = ref(true)
const petBusy = ref(false)
const petNote = ref('')
/** 这一句里的数字必须是在这台机器上量出来的；没量过就明说没量过，不写"更吃 CPU"这种废话。 */
/** 桥面报回来的当前像素框：底宽由它反推，界面这边不留第二份 420×640。 */
const petBox = ref<{ w: number; h: number }>({ w: 0, h: 0 })

/** 四档各是多少像素。这是代码里就定死的东西，写在界面上永远是真话。 */
const petBoxLine = computed(() => {
  const current = petScales.value.find((one) => one.id === petScale.value)
  if (!current || !petBox.value.w || !petBox.value.h) return '读不到当前尺寸'
  const baseW = petBox.value.w / current.multiplier
  const baseH = petBox.value.h / current.multiplier
  const words = petScales.value
    .map((one) => `${one.label} ${Math.round(baseW * one.multiplier)}×${Math.round(baseH * one.multiplier)}`)
    .join(' / ')
  return `${words}（现在：${current.label}）`
})

async function readPetSize(): Promise<void> {
  try {
    const answer = await fetchPetState()
    if (answer.scale) petScale.value = answer.scale
    if (Array.isArray(answer.scales) && answer.scales.length) petScales.value = answer.scales
    if (typeof answer.idle_motion === 'boolean') petIdle.value = answer.idle_motion
    if (answer.width && answer.height) petBox.value = { w: Number(answer.width), h: Number(answer.height) }
    if (answer.error) petNote.value = answer.error
  } catch (caught) {
    petNote.value = '读不到她多大：' + String(caught)
  }
}

async function chooseScale(id: string): Promise<void> {
  if (petBusy.value) return
  petBusy.value = true
  petNote.value = ''
  try {
    const answer = await setPetScale(id)
    petScale.value = answer.scale || id
    if (answer.error) petNote.value = answer.error
  } catch (caught) {
    petNote.value = '换大小没做成：' + String(caught)
  } finally {
    petBusy.value = false
  }
}

async function flipIdle(): Promise<void> {
  if (petBusy.value) return
  petBusy.value = true
  try {
    const answer = await setPetIdle(!petIdle.value)
    petIdle.value = typeof answer.idle_motion === 'boolean' ? answer.idle_motion : !petIdle.value
    if (answer.error) petNote.value = answer.error
  } catch (caught) {
    petNote.value = '那颗开关没改成：' + String(caught)
  } finally {
    petBusy.value = false
  }
}

async function readPets(): Promise<void> {
  try {
    const answer = await petAssets()
    if (!answer.ok) {
      petSummary.value = answer.error || '读不到'
      return
    }
    if (!answer.current) {
      petSummary.value = '小夜（默认）' + (answer.pets.length ? ' · 另存了 ' + answer.pets.length + ' 只' : '')
      return
    }
    const builtin = (answer.models ?? []).find((row) => row.id === answer.current)
    if (builtin) {
      petSummary.value = '内置模型：' + builtin.name + ' · ' + builtin.clips + ' 段动作'
      return
    }
    const chosen = answer.pets.find((row) => row.id === answer.current)
    petSummary.value = (chosen?.name ?? answer.current) + ' · ' + (chosen?.lines ?? '?') + ' 个拐点'
  } catch (caught) {
    petSummary.value = '读不到：' + String(caught)
  }
}

// Opened, or the picker closed while this panel stayed up: both mean the row may be
// lying about who is on the desktop.
watch(
  [() => props.open, () => props.petClosed],
  ([open]) => {
    if (open) {
      void readPets()
      void readPetSize()
      void readModelServer()
    }
  },
  { immediate: true },
)

const alertSustain = computed(() => original.value?.alerts?.sustain_seconds ?? 0)
const cooldownBounds = computed<[number, number]>(() => {
  const raw = original.value?.alerts?.cooldown_bounds ?? []
  return raw.length === 2 ? [raw[0], raw[1]] : [1, 180]
})
/**
 * The fields the engine owns, read off a snapshot rather than kept where the operator left them.
 *
 * One function for both the first load and every save, because after a refused patch the box
 * has to show what is actually in force -- a rejected wake-word list still sitting in the
 * input, or a threshold the machine is not watching, is the panel showing one line while the
 * engine draws another. That is the failure this section exists to avoid.
 */
function engineFormValues(snapshot: SettingsSnapshot): {
  alert_rules: Record<string, { enabled: boolean; threshold: number }>
  alert_cooldown: number
  alert_speak: boolean
  wake_keywords: string
} {
  const section = snapshot.alerts
  const stored = snapshot.wake_keywords_stored ?? []
  return {
    alert_rules: Object.fromEntries(
      (section?.rules ?? []).map((rule) => [
        rule.code,
        { enabled: rule.enabled, threshold: rule.threshold },
      ]),
    ),
    alert_cooldown: section?.cooldown_minutes ?? 10,
    alert_speak: section?.speak_critical ?? true,
    wake_keywords: (stored.length ? stored : (snapshot.wake_keywords ?? [])).join('、'),
  }
}
const editingRow = computed(() => models.value.find((row) => row.name === editing.value))
/** The models of the row being edited -- the same list the chat header's picker reads. */
const editingModels = computed<ModelSpec[]>(() => editingRow.value?.models ?? [])
/** 自带模型那一行：这一行的模型表由「选哪个离线模型」决定，界面上不给加也不给删。
 *  这个开关是后端算好送下来的（它才知道哪个名字是自带那一行），前端不抄 'local' 这个字面量。 */
const modelsLocked = computed(() => Boolean(editingRow.value?.models_locked))
/**
 * Marks a row the assistant changed and nobody has re-saved by hand.
 *
 * She is allowed to move a handful of her own settings; this is the condition that
 * makes that acceptable -- the change is not just reversible, it says who made it.
 */
function tag(key: string): string {
  return original.value?.ai_edited?.includes(key) ? ' · 小夜改的' : ''
}

const problemList = computed(() =>
  Object.entries(problems.value).map(([key, why]) => `${fieldLabel(key)}：${why}`),
)
const keyPlaceholder = computed(() =>
  editingRow.value?.key_set ? '已设置，输入新值可替换' : '粘贴你的 API Key',
)
/**
 * The row editor's 免 Key checkbox.
 *
 * ``null`` means "nobody touched it", which is what lets the save skip the field -- the
 * same convention as 调用地址 leaving blank means 不覆盖. Writing ``false`` on every save
 * would silently take the flag away from rows whose ``config.yaml`` says keyless.
 */
const rowKeyOptional = ref<boolean | null>(null)
/** The row editor's own timeout; null means untouched, '' means back to the global one. */
const rowTimeoutText = ref<string | null>(null)
const rowNoKey = computed({
  get: () => rowKeyOptional.value ?? Boolean(editingRow.value?.key_optional),
  set: (value: boolean) => {
    rowKeyOptional.value = value
  },
})
const draftKeyEnv = computed(() =>
  draft.value.name ? `${draft.value.name.toUpperCase().replace(/-/g, '_')}_API_KEY` : '—',
)
/** The section-wide timeout the rows fall back to, read from the shell like every bound. */
const globalTimeout = computed(() => original.value?.llm_timeout_seconds ?? 60)
/**
 * Loader menu straight from the shell. The ids used to be duplicated here as a label
 * table, which is exactly how a fifth backend loader would have rendered as a raw id.
 */
const loaderOptions = computed<{ id: string; label: string }[]>(() => {
  const shipped = original.value?.thinking_loader_options
  if (shipped?.length) return shipped
  return loaderChoices.value.map((id) => ({ id, label: loaderLabels[id] ?? id }))
})
const cloudVoiceKeyVariable = computed(() => original.value?.cloud_voice_key_variable ?? '—')
const cloudVoiceKeySet = computed(() => Boolean(original.value?.cloud_voice_key_set))
const cloudVoiceKey = ref('')
const saveWarning = ref('')
/** What GET /models came back with for the address in the add block. */
const endpointModels = ref<string[]>([])
const endpointModelsError = ref('')
const listingModels = ref(false)

/**
 * Ask the typed address which models it serves, so 模型清单 is a pick, not a copy job.
 *
 * The Key in the box goes along. It has to: this row does not exist yet, so there is no
 * saved credential for the shell to look up, and asking a gateway that guards ``/models``
 * without it came back as 「先把 Key 填上再拉」 with the key already typed one line above.
 */
async function fetchEndpointModels(): Promise<void> {
  listingModels.value = true
  endpointModels.value = []
  endpointModelsError.value = ''
  try {
    const answer = await listEndpointModels(draft.value.base_url, '', draft.value.api_key.trim())
    endpointModels.value = answer.models
    endpointModelsError.value = answer.ok ? '' : answer.error
  } catch (err) {
    endpointModelsError.value = err instanceof Error ? err.message : String(err)
  } finally {
    listingModels.value = false
  }
}

async function saveCloudVoiceKey(): Promise<void> {
  const value = cloudVoiceKey.value
  if (!value.trim()) return
  saving.value = true
  try {
    const result = await saveSettings({ api_key: value, key_for: 'voice_cloud' })
    original.value = result
    cloudVoiceKey.value = ''
    saveWarning.value =
      result.applied?.api_key === 'set_for_this_run_only'
        ? '钥匙只写进了本进程：写注册表那一步失败了，重启小夜后要重填'
        : ''
    savedNote.value = result.applied?.api_key ? '云端复刻 Key 已保存' : '没有保存上'
  } catch (err) {
    problems.value = { '云端复刻 Key': err instanceof Error ? err.message : String(err) }
  } finally {
    saving.value = false
  }
}

async function loadVoices(): Promise<void> {
  try {
    const list = await fetchVoices()
    voices.value = list
    voiceError.value = ''
  } catch (err) {
    voices.value = null
    voiceError.value = err instanceof Error ? err.message : String(err)
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const snapshot = await fetchSettings()
    original.value = snapshot
    editing.value = snapshot.provider
    rowKeyOptional.value = null
    rowTimeoutText.value = null
    form.value = {
      base_url: '',
      model: '',
      auto_speak_typed: snapshot.auto_speak_typed,
      wake_greeting: snapshot.wake_greeting,
      thinking_loader: snapshot.thinking_loader || 'dots',
      display_name: snapshot.display_name ?? '',
      local_model: snapshot.local_model ?? '',
      local_context: snapshot.local_context ?? 4096,
      local_idle_minutes: snapshot.local_idle_minutes ?? 10,
      local_eager: snapshot.local_eager ?? false,
      telemetry_interval_ms: snapshot.telemetry_interval_ms,
      thinking_enabled: snapshot.thinking_enabled,
      thinking_budget: snapshot.thinking_budget,
      history_turns: snapshot.history_turns,
      ...engineFormValues(snapshot),
    }
    lastPassed.value = snapshot.local_model ?? ''
    loadError.value = snapshot.error ?? ''
    await loadVoices()
  } catch (err) {
    loadError.value = `读取设置失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    loading.value = false
  }
}

/** Point the editor at another row; unsaved field edits are dropped on purpose. */
function editRow(name: string): void {
  if (name === editing.value) return
  editing.value = name
  if (form.value) {
    form.value.base_url = ''
    form.value.model = ''
    rowKeyOptional.value = null
  }
  apiKey.value = ''
  rowTimeoutText.value = null
  newModel.value = ''
  newLabel.value = ''
  modelNote.value = ''
}

/**
 * Add one model to the row being edited.
 *
 * A separate bridge call rather than part of the form's save: the model list is a
 * small change with its own verdict, and folding it into one big save would make a
 * rejected model name look like the whole form failed.
 */
/** The model row being measured, or empty. One at a time: it is a network call. */
const testing = ref('')
/** Verdicts from this visit only. The durable copy lives on the seat list in 对话. */
const verdicts = ref<Record<string, string>>({})

/** Ask one model the two questions. Results are written inline, never as a toast. */
async function testModel(modelId: string): Promise<void> {
  if (testing.value) return
  testing.value = modelId
  const key = `${editing.value}/${modelId}`
  try {
    const verdict = await llmTest(editing.value, modelId)
    verdicts.value = {
      ...verdicts.value,
      [key]: verdict.ok
        ? `能答，${Math.round(verdict.latency_ms)}ms；看图${
            verdict.vision === null ? '没测出来' : verdict.vision ? '可以' : '不行'
          }`
        : `连不上：${verdict.detail || '它一个字也没回'}`,
    }
  } catch (err) {
    verdicts.value = {
      ...verdicts.value,
      [key]: `测不了：${err instanceof Error ? err.message : String(err)}`,
    }
  } finally {
    testing.value = ''
  }
}

async function addModel(): Promise<void> {
  const wanted = newModel.value.trim()
  if (!wanted || saving.value) return
  saving.value = true
  modelNote.value = ''
  try {
    const result = await addProviderModel(editing.value, wanted, newLabel.value)
    if (!result.ok) {
      modelNote.value = result.error
      return
    }
    await reload()
    newModel.value = ''
    newLabel.value = ''
    modelNote.value = `已加上 ${wanted}`
    emit('saved', original.value as SettingsSnapshot)
  } catch (err) {
    modelNote.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

async function removeModel(modelId: string): Promise<void> {
  if (saving.value) return
  if (!window.confirm(`从「${editing.value}」里删掉模型 ${modelId}？`)) return
  saving.value = true
  modelNote.value = ''
  try {
    const result = await removeProviderModel(editing.value, modelId)
    if (!result.ok) {
      modelNote.value = result.error
      return
    }
    await reload()
    modelNote.value = `已删掉 ${modelId}`
    emit('saved', original.value as SettingsSnapshot)
  } catch (err) {
    modelNote.value = err instanceof Error ? err.message : String(err)
  } finally {
    saving.value = false
  }
}

/** Re-read the snapshot without moving the editor off the row it is on. */
async function reload(): Promise<void> {
  const snapshot = await fetchSettings()
  const keep = editing.value
  original.value = snapshot
  editing.value = keep || snapshot.provider
}

async function useRow(name: string): Promise<void> {
  await send({ provider: name }, `已切到 ${name}，下一句问答就用它`)
}

async function removeRow(name: string): Promise<void> {
  if (!window.confirm(`删掉界面添加的模型「${name}」？配置文件里的行不受影响。`)) return
  await send({ remove_model: name }, `已删除 ${name}`)
}

/** Hide a row that came from the file. Reversible, and it never touches config.yaml. */
async function hideRow(name: string): Promise<void> {
  await send({ hide_provider: name }, `已把 ${name} 藏起来，下面「找回」它`)
}

async function showRow(name: string): Promise<void> {
  await send({ show_provider: name }, `${name} 回来了`)
}

async function send(patch: Record<string, unknown>, note: string): Promise<void> {
  if (saving.value) return
  saving.value = true
  problems.value = {}
  savedNote.value = ''
  try {
    const result = await saveSettings(patch)
    original.value = result
    if (form.value) Object.assign(form.value, engineFormValues(result))
    problems.value = result.problems ?? {}
    if (!Object.keys(result.problems ?? {}).length) savedNote.value = note
    if (result.models) editing.value = result.provider || editing.value
    emit('saved', result)
  } catch (err) {
    problems.value = { 保存: err instanceof Error ? err.message : String(err) }
  } finally {
    saving.value = false
  }
}

/** 保存之后那句回读。键名是英文的，人不该为了看懂"刚才到底存了什么"去读代码；
 *  认不出的键照原样印出来，那比编一个中文名字更诚实。 */
const FIELD_LABELS: Record<string, string> = {
  auto_speak_typed: '打字也念出来',
  wake_greeting: '唤醒问候语',
  wake_keywords: '唤醒词',
  thinking_loader: '思考动画',
  display_name: '她叫什么',
  local_model: '选哪个离线模型',
  local_context: '自带模型上下文',
  local_idle_minutes: '多久关掉自带模型',
  local_eager: '内存不够也加载',
  telemetry_interval_ms: '遥测间隔',
  thinking_enabled: '深度思考',
  thinking_budget: '思考预算',
  history_turns: '上下文轮数',
  alerts_rules: '告警规则',
  alerts_cooldown_minutes: '重复提醒间隔',
  alerts_speak_critical: '严重告警用语音提醒',
  base_url: '调用地址',
  model: '起始模型',
  key_optional: '免 Key 标记',
  api_key: 'API Key',
  add_model: '新增服务商',
  remove_model: '删掉服务商',
  provider: '当前服务商',
  hide_provider: '藏起来',
  show_provider: '找回',
}

function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key
}

async function save() {
  if (!form.value || saving.value) return
  const patch: Record<string, unknown> = {
    target: editing.value,
    auto_speak_typed: form.value.auto_speak_typed,
    wake_greeting: form.value.wake_greeting.trim(),
    wake_keywords: form.value.wake_keywords.trim(),
    thinking_loader: form.value.thinking_loader,
    display_name: form.value.display_name,
    local_context: form.value.local_context,
    local_idle_minutes: form.value.local_idle_minutes,
    local_eager: form.value.local_eager,
    telemetry_interval_ms: form.value.telemetry_interval_ms,
  }
  // 这三格改成"跟当前这一对模型"之后，照发就等于每次保存都把它钉住 —— 而且面板打开之后
  // 要是有人在对话区换了模型，发出去的就是另一对的旧值（r79 那条规矩：没动过的格不发）。
  if (form.value.thinking_enabled !== Boolean(original.value?.thinking_enabled)) {
    patch.thinking_enabled = form.value.thinking_enabled
  }
  if (form.value.thinking_budget !== Number(original.value?.thinking_budget ?? 0)) {
    patch.thinking_budget = form.value.thinking_budget
  }
  if (form.value.history_turns !== Number(original.value?.history_turns ?? 0)) {
    patch.history_turns = form.value.history_turns
  }
  // 没动过的那一格不发出去。这一格的值来自"读设置"那一次调用，万一读到的和后端此刻在用的
  // 不是一份（真机抓到过一次），照发就会把用户选的离线模型悄悄换成包里最大的那个。
  if (form.value.local_model !== (original.value?.local_model ?? '')) {
    patch.local_model = form.value.local_model
  }
  if (alertRules.value.length) {
    patch.alerts_rules = form.value.alert_rules
    patch.alerts_cooldown_minutes = form.value.alert_cooldown
    patch.alerts_speak_critical = form.value.alert_speak
  }
  if (form.value.base_url) patch.base_url = form.value.base_url
  if (form.value.model) patch.model = form.value.model
  // Only a checkbox the operator actually moved goes out, for the same reason 调用地址
  // sends nothing when left blank: re-sending ``false`` on every save would take the
  // 免 Key flag away from rows whose config.yaml already says the endpoint needs no key.
  const noKey = rowKeyOptional.value
  if (noKey !== null && noKey !== Boolean(editingRow.value?.key_optional)) {
    patch.key_optional = noKey
  }
  if (rowTimeoutText.value !== null && rowTimeoutText.value !== '') {
    patch.timeout_seconds = rowTimeoutText.value
  } else if (rowTimeoutText.value === '' && editingRow.value?.timeout_seconds != null) {
    patch.timeout_seconds = ''
  }
  if (apiKey.value.trim()) {
    patch.api_key = apiKey.value
    patch.key_for = editing.value
  }
  // The row still being typed goes with the queue when it is complete: an operator who
  // filled four fields and pressed 保存 instead of 加进待添加 must not lose it. When it is
  // not complete it stays on screen, and only the queued rows go out.
  const queued = pendingProviders.value.map(providerPayload)
  const draftComplete = draftReady.value
  if (queued.length) {
    patch.add_model = draftComplete ? [...queued, providerPayload(draft.value)] : queued
  } else if (draft.value.name && (draft.value.base_url || draftedModels.value.length)) {
    patch.add_model = providerPayload(draft.value)
  }
  saving.value = true
  problems.value = {}
  savedNote.value = ''
  try {
    const result = await saveSettings(patch)
    original.value = result
    if (form.value) Object.assign(form.value, engineFormValues(result))
    problems.value = result.problems ?? {}
    // A top-level refusal (settings unavailable, patch not a mapping) used to arrive as
    // an empty problems map and read as 「没有字段被改动」 -- the worst possible wording
    // for "nothing you asked for happened".
    if (result.error) problems.value = { ...problems.value, 保存: result.error }
    const done = Object.keys(result.applied ?? {}).length
    savedNote.value = done
      ? `已保存并立即生效：${Object.keys(result.applied ?? {}).map(fieldLabel).join('、')}`
      : '没有字段被改动'
    // The gate can save a row it never tested (no probe wired), and a key can land in
    // this process only when the registry write fails. Both used to be invisible behind
    // 「已保存并立即生效」, the one sentence the operator trusts.
    saveWarning.value = result.warning
      ? result.warning
      : result.applied?.api_key === 'set_for_this_run_only'
        ? '钥匙只写进了本进程：写注册表那一步失败了，重启小夜后要重填'
        : ''
    if (result.applied?.api_key) apiKey.value = ''
    if (result.applied?.add_model) {
      pendingProviders.value = []
      if (draftComplete || queued.length === 0) draft.value = { ...BLANK_PROVIDER_DRAFT }
    }
    form.value.base_url = ''
    form.value.model = ''
    rowKeyOptional.value = null
    rowTimeoutText.value = null
    emit('saved', result)
  } catch (err) {
    problems.value = { 保存: err instanceof Error ? err.message : String(err) }
  } finally {
    saving.value = false
  }
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) void load()
  },
)
/** The picker is a different window reading the same list: re-read it when that one
 *  closes, or this row keeps naming the voice the operator just replaced. */
watch(
  () => props.voiceClosed,
  () => {
    if (props.open) void loadVoices()
  },
)
</script>

<style scoped>
.gate {
  position: absolute;
  inset: 0;
  z-index: 3;
  display: grid;
  place-items: center;
  background: rgba(2, 5, 10, 0.78);
}

.gate__card {
  width: min(560px, 82%);
  padding: 16px 18px;
  border: 1px solid rgba(120, 210, 255, 0.55);
  border-radius: 12px;
  background: rgba(6, 14, 24, 0.98);
  box-shadow: 0 0 26px rgba(60, 180, 255, 0.28);
}

.gate__title {
  margin: 0 0 8px;
  color: #ff9c6b;
  font-size: 15px;
}

.gate__why {
  margin: 0 0 8px;
  color: #ffd7a8;
  font-size: 13px;
  line-height: 1.7;
}

.gate__nums,
.gate__hard {
  margin: 0 0 10px;
  color: #8fa6b8;
  font-size: 12px;
  line-height: 1.7;
}

.gate__hard {
  color: #ff9c6b;
}

.gate__row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.settings {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.settings__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.settings__box {
  position: relative;
  width: min(600px, 92vw);
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  padding: 18px 20px 20px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.settings__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.settings__body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  flex: 1 1 auto;
  min-height: 0;
}

.settings__scroll {
  display: flex;
  flex-direction: column;
  gap: 14px;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.settings__group {
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  padding: 10px 12px 12px;
}

.settings__group legend {
  padding: 0 6px;
  color: var(--hud-cyan);
  font-size: 11px;
  letter-spacing: 0.18em;
}

.settings__models {
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.settings__model {
  display: grid;
  grid-template-columns: 110px 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 4px 7px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
}

.settings__model--on {
  border-color: rgba(77, 216, 255, 0.4);
  background: rgba(77, 216, 255, 0.07);
}

.settings__model--edit {
  border-color: rgba(255, 181, 71, 0.5);
}

.settings__model-pick {
  border: none;
  background: transparent;
  color: var(--hud-cyan);
  font-size: 12px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings__model-meta {
  color: var(--hud-dim);
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings__model-ops {
  display: flex;
  gap: 5px;
}

.settings__del {
  color: var(--hud-red);
}

/*
 * The per-provider model list. Its own bordered block rather than another form row:
 * it is a set of things, not a value, and drawing it as one more input would hide the
 * fact that the chat dropdown reads exactly these rows.
 */
.settings__mlist {
  margin-top: 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.settings__mrow {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 3px;
  max-height: 180px;
  overflow-y: auto;
}

.settings__mitem {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 4px;
  border-radius: var(--hud-radius);
  background: rgba(77, 216, 255, 0.04);
}

.settings__mname {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--hud-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings__mid {
  flex: none;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings__mtag {
  color: var(--hud-cyan);
}

.settings__madd {
  display: flex;
  gap: 6px;
  align-items: center;
}

.settings__minput {
  flex: 1;
  min-width: 0;
}

.settings__add {
  margin-top: 10px;
  border-top: 1px dashed var(--hud-line);
  padding-top: 8px;
}

.settings__add summary {
  cursor: pointer;
  color: var(--hud-cyan);
  font-size: 11px;
  letter-spacing: 0.06em;
}

.settings__rule {
  margin-top: 10px;
  padding-top: 6px;
  border-top: 1px dashed rgba(120, 190, 220, 0.22);
}

.settings__small {
  width: 92px;
}

.settings__row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
}

.settings__row--inline {
  flex-direction: row;
  align-items: center;
  gap: 8px;
}

.settings__row input[type='text'],
.settings__row input[type='password'],
.settings__row input[type='number'],
.settings__row select {
  /* Same box as .hud-field in hud.css: one control shape for the whole window. */
  height: var(--hud-control-h);
  padding: 0 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-pill);
  background: rgba(3, 9, 16, 0.8);
  color: var(--hud-text);
  font-size: 12px;
  font-family: inherit;
  outline: none;
}

.settings__keyline {
  display: flex;
  gap: 6px;
}

.settings__keyline input {
  flex: 1;
  min-width: 0;
}

.settings__why {
  margin: 4px 0 0;
  color: var(--hud-dim);
  font-size: 11px;
  line-height: 1.5;
}

/* The reason 「加进待添加」 is not clickable, next to the button rather than in a toast:
   a disabled control with no stated reason reads as a broken feature. */
.settings__why--bad {
  color: var(--hud-red);
  align-self: center;
}

.settings__add-ops {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

/* The presets go above the fields rather than below them: a person who came here to
   point the app at a local model should not have to read four labels first to find it. */
.settings__presets {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin: 2px 0 8px;
  font-size: 11px;
  color: var(--hud-dim);
}

/*
 * Which of the endpoint's own model names are already on this row. Without a marked state
 * a second click looks like it did nothing -- the list is right there, and the one thing
 * you cannot tell is whether the name you just tapped is in or out.
 */
.settings__presets .hud-btn--on {
  border-color: var(--hud-cyan);
  color: var(--hud-cyan);
}

.settings__queue {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.settings__queue li {
  display: grid;
  grid-template-columns: minmax(0, 96px) minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

/* The address is the long part, so it is the one allowed to truncate -- the name and the
   去掉 button are what the operator needs to hit. */
.settings__queue-url {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--hud-dim);
}

.settings__queue-count {
  margin-left: 6px;
  color: var(--hud-amber);
}

.settings__state {
  color: var(--hud-amber);
  font-size: 12px;
}

.settings__saved {
  margin: 0;
  color: var(--hud-cyan);
  font-size: 12px;
}

.settings__problems {
  margin: 0;
  padding-left: 18px;
  color: var(--hud-red);
  font-size: 12px;
}

.settings__error,
.settings__loading {
  color: var(--hud-amber);
  font-size: 12px;
}

.settings__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

code {
  color: var(--hud-cyan);
}
</style>
