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
            点名字编辑那一行的地址和模型名；「设为当前」立刻生效，不用重启。
            配置文件里的行删不掉、也改不回文件——界面上能改的是这一行怎么用，
            要真删请自己开 config.yaml。
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
            <div class="settings__madd">
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
            <p v-if="modelNote" class="settings__why">{{ modelNote }}</p>
          </div>

          <label class="settings__row">
            <span class="hud-label">调用地址</span>
            <input v-model.trim="form.base_url" type="text" spellcheck="false" :placeholder="editingRow?.base_url" />
          </label>
          <p class="settings__why">留空 = 不覆盖，用这一行本来的地址。</p>

          <label class="settings__row">
            <span class="hud-label">起始模型（该服务商默认用哪个）</span>
            <input v-model.trim="form.model" type="text" spellcheck="false" :placeholder="editingRow?.default_model" />
          </label>
          <p class="settings__why">对话区第一次选到这个服务商时用它；留空 = 不改这一行（和调用地址同一个道理）。</p>

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
            状态 {{ editingRow?.key_set ? '已设置' : '未设置' }}。
            密钥只写进环境变量（本进程立刻生效 + Windows 用户环境以便重启后仍在），
            不写进任何配置文件，也不会回显到页面上。
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
            现在这一行是「{{ editingRow?.key_optional ? '免 Key' : '要 Key' }}」。勾上就不发
            Authorization 头，本机 Ollama、LM Studio、llama.cpp 自带的 server 都是这样；
            环境里存着钥匙的话照样会用。不勾的话没钥匙会被客户端在发请求之前直接拒掉。
          </p>

          <details class="settings__add">
            <summary class="hud-label">
              ＋ 添加服务商（一次可以加几家）
              <b v-if="pendingProviders.length" class="hud-num settings__queue-count">
                待添加 {{ pendingProviders.length }}
              </b>
            </summary>
            <p class="settings__why">
              下面这四格说的是一家**新的**服务商（一个新地址、一把新钥匙）。
              已经在这张表里的服务商，点它的名字进去改地址、改起始模型、加它的其它模型。
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
              <span class="hud-label">起始模型</span>
              <input v-model.trim="draft.model" type="text" spellcheck="false" placeholder="deepseek-chat" />
            </label>
            <div class="settings__add-ops">
              <button
                class="hud-btn"
                type="button"
                :disabled="!/^https?:\/\//.test(draft.base_url) || listingModels"
                :title="listingModels ? '在问它要清单' : '对它发一次 GET /models，把它自己列的模型名拿回来'"
                @click="fetchEndpointModels"
              >
                {{ listingModels ? '问它要清单…' : '看看它有哪些模型' }}
              </button>
              <span v-if="endpointModelsError" class="settings__why settings__why--bad">
                {{ endpointModelsError }}
              </span>
            </div>
            <div v-if="endpointModels.length" class="settings__presets">
              <span class="hud-label">它列出来的模型（点一下填进起始模型）</span>
              <button
                v-for="id in endpointModels"
                :key="id"
                class="hud-btn"
                type="button"
                @click="draft.model = id"
              >
                {{ id }}
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
            <p class="settings__why">先是这一个；加好这个服务商后，在上面那一行里继续加它别的模型。</p>
            <label class="settings__row settings__row--inline">
              <input v-model="draft.key_optional" type="checkbox" />
              <span class="hud-label">这个端点不需要 API Key（本地模型 / 内网服务）</span>
            </label>
            <p v-if="draft.key_optional" class="settings__why">
              勾上就不发 Authorization 头。Ollama、LM Studio、llama.cpp 自带的 server 都是这样；
              不勾的话没钥匙会被客户端在发请求之前直接拒掉。
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
                <span class="settings__queue-url">{{ row.base_url }} · {{ row.model }}</span>
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
            状态 {{ cloudVoiceKeySet ? '已设置' : '未设置' }}。只有勾了「传到厂商做云端复刻」
            才会用到它；不勾就一次都不会发出去。和别家的钥匙一样只进环境变量。
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
            「思考中」文案左边那种动效。对话气泡和桌面人物头上那张卡一起换 ——
            同一个等待不该长成两个样子。
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
            说「你好小夜」之后她先回的这一句。要等桌面人物完全显形才开口；
            宠物没开、或者主界面正开着的时候不等，直接说。清空就是不说。
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
            说其中任何一个就算叫她。改完**下一句就生效**，不用重开麦克风 ——
            重启会把正在说的半句话吃掉。用顿号或空格分开，最多
            {{ wakeKeywordMax }} 个、每个 {{ wakeKeywordMinChars }}–{{ wakeKeywordMaxChars }} 个字：
            太短会在日常说话里撞到，太长是句子不是名字。
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
            <span class="hud-label">麦克风自动待命</span>
            <span class="settings__state">
              {{ original?.voice_auto_arm ? '开机会自己回到待唤醒' : '开机不自动开麦' }}
            </span>
          </div>
          <p class="settings__why">
            这一项由窗口右上角的「启用语音」/「释放麦克风」控制，不在这里改：
            开麦的同意必须来自那一次按压。
          </p>
        </fieldset>

        <fieldset class="settings__group">
          <legend>思考与上下文</legend>
          <label class="settings__row settings__row--inline">
            <input v-model="form.thinking_enabled" type="checkbox" />
            <span class="hud-label">要她给出思考过程{{ tag('thinking_enabled') }}</span>
          </label>
          <p class="settings__why">
            开了之后每条回答下面会出现可展开的「思考过程」。这是向模型额外要的 token，
            不要就关掉；没开的模型不回这段，框也就是空的。
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
            {{ budgetBounds[0] }}–{{ budgetBounds[1] }} 之间。这是真的预算不是「高/中/低」：
            到数了她就停止思考直接答，回答里的思考 token 数能和这个值对上，所以能验证它没变成摆设。
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
            每次提问带上前几轮对话，{{ turnBounds[0] }}–{{ turnBounds[1] }} 之间，0 表示只看这一句。
            调大能接得上「接着刚才那个说」，但每一轮都要把这段重付一遍。
          </p>
        </fieldset>

        <fieldset v-if="alertRules.length" class="settings__group">
          <legend>告警</legend>
          <p class="settings__why">
            每一行是一条线：连续超过 {{ alertSustain }} 秒才算一次，恢复了才会关掉。
            关掉某一行只是不再盯它，不影响概览里的其它读数。
          </p>
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
  removeProviderModel,
  saveSettings,
  type AlertRuleSetting,
  type HiddenRow,
  type ModelRow,
  type ModelSpec,
  type SettingsSnapshot,
  type VoiceList,
} from '@/api/bridge'

const props = defineProps<{ open: boolean; voiceClosed?: number }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved', snapshot: SettingsSnapshot): void; (e: 'voice'): void }>()

const original = ref<SettingsSnapshot | null>(null)
const form = ref<{
  base_url: string
  model: string
  auto_speak_typed: boolean
  wake_greeting: string
  wake_keywords: string
  thinking_loader: string
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
  model: string
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

/** The three rules the save enforces, checked here so 「加进待添加」 is not a button that
 *  looks dead. Names are matched against the rows on screen *and* against the queue:
 *  two pending rows with one name come back as one row, silently. */
const draftProblem = computed(() => {
  const row = draft.value
  if (!row.name) return '先填名字'
  if (!/^[a-z0-9_-]{1,32}$/.test(row.name)) return '名字只能是小写字母、数字、- 和 _，1-32 位'
  if (!/^https?:\/\//.test(row.base_url)) return '地址必须以 http:// 或 https:// 开头'
  if (!row.model) return '还要填起始模型'
  if (row.model.includes(' ') || row.model.length > 150) return '模型名不能含空格且不超过 150 字符'
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
  Boolean(draft.value.name || draft.value.base_url || draft.value.model),
)

function queueProvider(): void {
  if (!draftReady.value) return
  pendingProviders.value.push({ ...draft.value })
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

/** Ask the typed address which models it serves, so 起始模型 is a pick, not a copy job. */
async function fetchEndpointModels(): Promise<void> {
  listingModels.value = true
  endpointModels.value = []
  endpointModelsError.value = ''
  try {
    const answer = await listEndpointModels(draft.value.base_url)
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
      telemetry_interval_ms: snapshot.telemetry_interval_ms,
      thinking_enabled: snapshot.thinking_enabled,
      thinking_budget: snapshot.thinking_budget,
      history_turns: snapshot.history_turns,
      ...engineFormValues(snapshot),
    }
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
    telemetry_interval_ms: form.value.telemetry_interval_ms,
    thinking_enabled: form.value.thinking_enabled,
    thinking_budget: form.value.thinking_budget,
    history_turns: form.value.history_turns,
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
  const queued = pendingProviders.value.map((row) => ({ ...row }))
  const draftComplete = draftReady.value
  if (queued.length) {
    patch.add_model = draftComplete ? [...queued, { ...draft.value }] : queued
  } else if (draft.value.name && (draft.value.base_url || draft.value.model)) {
    patch.add_model = { ...draft.value }
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
  overflow: auto;
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
