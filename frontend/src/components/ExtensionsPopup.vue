<template>
  <div v-if="open" class="ext" role="dialog" aria-modal="true" aria-label="扩展">
    <div class="ext__scrim" @click="emit('close')"></div>

    <section class="ext__box">
      <header class="ext__head">
        <h2 class="hud-title">扩展 · 插件市场</h2>
        <div class="ext__tabs">
          <button
            v-for="entry in TABS"
            :key="entry.id"
            class="hud-btn"
            :class="{ 'hud-btn--primary': entry.id === tab }"
            type="button"
            @click="select(entry.id)"
          >
            {{ entry.label }}
          </button>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </div>
      </header>

      <div class="ext__body">
        <!-- 后端回来的每一句 error 都要上屏，不许咽进 console：这一窗口的规矩是「失败看得见」。 -->
        <p v-if="notice" class="ext__note" :class="{ 'ext__note--bad': noticeBad }">{{ notice }}</p>
        <p v-if="state.error" class="ext__error">{{ state.error }}</p>
        <p v-if="state.store_error" class="ext__error">servers.json 这一格读不了：{{ state.store_error }}</p>
        <!-- 「还没读到」和「这台机器什么都没有」是两句话，混在一格上就是又一处假安静。 -->
        <p v-if="!loaded" class="ext__hint">还没读到这一层，所以上面下面都是空的。这不是「这台机器没装过任何东西」。</p>

      <!-- ========================================================= 搜索 -->
      <template v-if="tab === 'search'">
        <form class="ext__form" @submit.prevent="runSearch">
          <input v-model="query" class="hud-field ext__q" placeholder="想让她多会什么，例如 pdf / 数据库 / 日历" />
          <button class="hud-btn" type="button" :disabled="searching" @click="curatedOnly = !curatedOnly">
            {{ curatedOnly ? '只看作者验过的那份（已选）' : '只看作者验过的那份' }}
          </button>
          <button class="hud-btn hud-btn--primary" type="submit" :disabled="searching || nothingChecked">
            {{ searching ? '搜索中…' : '搜一下' }}
          </button>
        </form>

        <div class="ext__kinds">
          <label class="ext__kind">
            <input v-model="wantSkill" type="checkbox" />
            <span class="hud-label">技能（只是说明书）</span>
          </label>
          <label class="ext__kind">
            <input v-model="wantMcp" type="checkbox" />
            <span class="hud-label">MCP server（进程外）</span>
          </label>
          <span class="ext__hint">{{ kindsHint }}</span>
        </div>

        <!-- 装之前那张卡：到底会发生什么，必须在这一次点击之前摊开写清楚 —— 所以它跟着按钮那一行，
             而不是排在三十条结果底下。 -->
        <div v-if="pending" class="ext__confirm">
          <p class="ext__confirm-title">确认要装这一条吗：{{ pending.name }}</p>
          <template v-if="pending.kind === 'mcp'">
            <p class="ext__hint">这会往下面那个文件里登记一条服务，之后由小夜<strong>以外</strong>的一个独立进程把它跑起来，用的是<strong>你当前账户的权限</strong>。</p>
            <p class="ext__hint">它会执行的完整命令行是这一条，看清楚再按：</p>
            <p class="ext__cmd">{{ commandLine(pending) }}</p>
            <p class="ext__hint">登记落在：{{ state.servers_file || '（这一层没报出文件）' }}</p>
          </template>
          <template v-else>
            <p class="ext__hint">这是一份说明书（只有文本，不会被执行）。它会被写进这一个目录，只动这一个目录：</p>
            <p class="ext__cmd">{{ skillTarget(pending) }}</p>
            <p v-if="pending.pinned" class="ext__hint">来源钉在 {{ pending.pinned }}，不会跟着上游改版漂走。</p>
          </template>
          <p v-if="needsWords(pending) !== '没有特别要求'" class="ext__hint">这一条跑起来要本机有：{{ needsWords(pending) }}</p>
          <div class="ext__confirm-actions">
            <button class="hud-btn hud-btn--primary" type="button" :disabled="installing || !!installBlock(pending)" @click="confirmInstall">
              {{ installing ? '正在装…' : '确认装上' }}
            </button>
            <button class="hud-btn" type="button" :disabled="installing" @click="pending = null">算了</button>
          </div>
        </div>

        <!-- 详情卡：点图标或点名字弹出来，盖在这一格上面。**不另开一扇窗** ——
             桌面已经有设置 40 / 助手 44 / 扩展 46 三层，再开一扇就要再付一次遮挡与关闭的账。 -->
        <div v-if="detail" class="ext__detail" data-testid="ext-detail">
          <div class="ext__detail-head">
            <span class="ext__icon ext__icon--big">
              <img v-if="pictureOf(detailOf)" :src="pictureOf(detailOf)" alt="" @error="markBroken(detailOf)" />
              <span v-else class="ext__icon-mono">{{ monogram(detailOf.name) }}</span>
            </span>
            <div class="ext__detail-titles">
              <span class="ext__name">{{ detailOf.name || detail.id }}</span>
              <span class="hud-label">
                {{ kindWord(detailOf.kind) }} · 来自 {{ detailOf.source || '？' }}
                <template v-if="detailOf.version"> · 版本 {{ detailOf.version }}</template>
              </span>
            </div>
            <button class="hud-btn" type="button" @click="closeDetail">收起详情</button>
          </div>

          <p class="ext__summary">{{ detailOf.description || detailOf.summary || '这条没写它是干什么的。' }}</p>
          <p class="ext__meta">{{ iconWords(detailOf) }}</p>
          <p v-if="detailOf.homepage" class="ext__meta">
            出处：<a class="ext__link" :href="detailOf.homepage" target="_blank" rel="noreferrer">{{ detailOf.homepage }}</a>
          </p>
          <p class="ext__meta">这一条跑起来要本机有：{{ needsWords(detailOf) }}</p>

          <template v-if="detailOf.kind === 'mcp'">
            <p class="ext__hint">它由小夜<strong>以外</strong>的独立进程跑起来，用的是<strong>你当前账户的权限</strong>。要执行的完整命令行是这一条：</p>
            <p class="ext__cmd">{{ commandLine(detailOf) }}</p>
            <p class="ext__meta">登记落在：{{ detail.servers_file || '（这一层没报出文件）' }}</p>
          </template>
          <template v-else>
            <p class="ext__hint">这是一份说明书（只有文本，不会被执行），它落在这一个目录：</p>
            <p class="ext__cmd">{{ skillTarget(detailOf) }}</p>
          </template>
          <p v-if="detailOf.pinned" class="ext__meta">来源钉在 {{ detailOf.pinned }}</p>
          <p class="ext__note">{{ detail.installed.words || '还没装' }}</p>
          <p v-if="detail.installed.path" class="ext__meta">装在哪：{{ detail.installed.path }}</p>

          <div class="ext__confirm-actions">
            <button
              v-if="!detail.installed.yes"
              class="hud-btn hud-btn--primary"
              type="button"
              :disabled="!detail.install.can || busy"
              @click="askInstall(detailOf)"
            >
              {{ installing ? '正在装…' : '装上' }}
            </button>
            <button
              v-else
              class="hud-btn"
              type="button"
              :disabled="!detail.update.can || busy"
              :title="detail.update.words"
              @click="doUpdate(detailOf)"
            >
              {{ updating ? '正在更新…' : '更新' }}
            </button>
            <button
              v-if="detail.installed.yes && pendingRemove !== detail.id"
              class="hud-btn"
              type="button"
              :disabled="busy"
              @click="pendingRemove = detail.id"
            >
              删掉
            </button>
            <template v-else-if="detail.installed.yes">
              <span class="ext__warn">{{ removeWords(detailOf) }}</span>
              <button class="hud-btn danger" type="button" :disabled="busy" @click="confirmRemove(detailOf)">确认删掉</button>
              <button class="hud-btn" type="button" :disabled="busy" @click="pendingRemove = ''">算了</button>
            </template>
          </div>
          <p v-if="!detail.installed.yes && !detail.install.can" class="ext__warn">装不了：{{ detail.install.words }}</p>
          <p v-if="detail.installed.yes && !detail.update.can" class="ext__warn">这条更新不了：{{ detail.update.words }}</p>
          <p v-if="detail.error" class="ext__error">{{ detail.error }}</p>
        </div>

        <!-- 没搜过时这一屏摆的是<strong>推荐</strong>；搜过之后是三路结果。
             两者走同一个 panels 渲染 —— 行模板写两份，早晚会有一份忘了改。 -->
        <p v-if="!searched" class="ext__hint">
          还没搜过，所以这里摆的是<strong>推荐</strong>：作者逐条验过能装的那几条，不用先想关键词。
          上面框里打一个字再按「搜一下」就换成三路搜索的结果。
        </p>
        <p v-if="searched && !search.items.length && !search.failures.length" class="ext__empty">
          三路都问到了，但市场里没有对得上的条目。换个词再试一次。
        </p>
        <p v-if="search.error" class="ext__error">{{ search.error }}</p>

        <section v-for="panel in panels" :key="panel.source" class="ext__group">
          <div class="ext__group-head">
            <span class="hud-label ext__group-name">{{ panel.label }}</span>
            <span class="hud-chip" :class="{ 'hud-chip--bad': !!panel.error }">
              {{ panel.hits }} 条<template v-if="panel.milliseconds"> · {{ panel.milliseconds }} 毫秒</template>
            </span>
          </div>
          <!-- 失败原文照抄，不许折成「没有结果」：「市场里没有」和「我这网不通」改的是两根不同的线。 -->
          <p v-if="panel.error" class="ext__error">这一路没通：{{ panel.error }}</p>
          <ul v-if="panel.items.length" class="ext__list">
            <li v-for="item in panel.items" :key="item.id" class="ext__row">
              <button class="ext__icon" type="button" :title="'看详情：' + item.name" @click="openDetail(item)">
                <img v-if="pictureOf(item)" :src="pictureOf(item)" alt="" @error="markBroken(item)" />
                <span v-else class="ext__icon-mono">{{ monogram(item.name) }}</span>
              </button>
              <div class="ext__row-main">
                <button class="ext__name ext__name--link" type="button" @click="openDetail(item)">{{ item.name }}</button>
                <span class="ext__summary">{{ item.summary }}</span>
                <span class="ext__meta">{{ kindWord(item.kind) }} · 来自 {{ item.source }} · 要本机装：{{ needsWords(item) }}</span>
                <span v-if="item.pinned" class="ext__meta">来源钉在 {{ item.pinned }}</span>
                <span v-if="installedWords(item)" class="ext__note">{{ installedWords(item) }}</span>
                <p v-if="installBlock(item)" class="ext__warn">装不了：{{ installBlock(item) }}</p>
                <p v-if="updateBlock(item)" class="ext__warn">{{ updateBlock(item) }}</p>
              </div>
              <a v-if="item.homepage" class="hud-chip ext__link" :href="item.homepage" target="_blank" rel="noreferrer">出处</a>
              <button class="hud-btn" type="button" :disabled="busy" @click="openDetail(item)">详情</button>
              <button
                v-if="!isInstalled(item)"
                class="hud-btn hud-btn--primary"
                type="button"
                :disabled="!!installBlock(item) || busy"
                @click="askInstall(item)"
              >
                装上
              </button>
              <template v-else>
                <button
                  class="hud-btn"
                  type="button"
                  :disabled="!canUpdate(item) || busy"
                  :title="updateWhy(item)"
                  @click="doUpdate(item)"
                >
                  {{ updating ? '正在更新…' : '更新' }}
                </button>
                <button
                  v-if="pendingRemove !== item.id"
                  class="hud-btn"
                  type="button"
                  :disabled="busy"
                  @click="pendingRemove = item.id"
                >
                  删掉
                </button>
                <template v-else>
                  <span class="ext__warn">{{ removeWords(item) }}</span>
                  <button class="hud-btn danger" type="button" :disabled="busy" @click="confirmRemove(item)">确认删掉</button>
                  <button class="hud-btn" type="button" :disabled="busy" @click="pendingRemove = ''">算了</button>
                </template>
              </template>
            </li>
          </ul>
        </section>

        <!-- 确认卡在搜索结果**上面**（紧挨着那一行勾选）：结果一长，卡就掉到视口外，
             「装之前把命令行摊开看清楚」就成了句没人看见的话。本轮打包版实测抓出来的。 -->

        <p v-if="installResult" class="ext__note" :class="{ 'ext__note--bad': !installResult.ok }">
          {{ installResult.ok ? `装上了：${installResult.name}${installResult.connect ? ' · ' + installResult.connect : ''}` : `没装上：${installResult.reason || '原因见上面那一句'}` }}
        </p>

        <p class="ext__edge">这个市场只卖两种东西：技能（只是说明书，不执行）和 MCP 服务（跑在小夜外面的独立进程）。<strong>会跑的 Python 代码包不在这里卖</strong> —— 从网上下代码 import 进小夜自己，等于给每台装了它的机器开一条远程代码执行通道，出事没人负责。</p>
      </template>

      <!-- ========================================================= 已装 -->
      <template v-else-if="tab === 'installed'">
        <section class="ext__layer">
          <div class="ext__headrow">
            <span class="hud-label">允许她运行外部扩展程序</span>
            <button class="hud-btn" :class="{ 'hud-btn--primary': state.layer_enabled }" type="button" :disabled="busy" @click="flipLayer">
              {{ busy ? '写入中…' : state.layer_enabled ? '现在：开着' : '现在：关着' }}
            </button>
          </div>
          <p class="ext__hint">开了它，登记的 MCP server 会作为独立程序在小夜外面跑，用的是<strong>当前账户的权限</strong> —— 它能读你读得到的一切。关了就一条都不连，已经装上的东西还在盘上，随时能再开。</p>
        </section>

        <!-- ------------------------------------------------------ 技能 -->
        <h3 class="hud-label ext__section">技能 · {{ state.skills.length }} 份</h3>
        <p v-if="!state.skills.length" class="ext__empty">还没有装过任何东西。到「搜索」那一格勾上「技能」搜一条；技能只是一份说明书，不装也不影响她别的本事。</p>
        <ul v-else class="ext__list">
          <li v-for="row in state.skills" :key="row.slug" class="ext__row" :class="{ 'ext__row--off': !row.enabled }">
            <span class="hud-chip">{{ row.enabled ? '启用' : '关着' }}</span>
            <div class="ext__row-main">
              <span class="ext__name">{{ row.name }}</span>
              <span class="ext__summary">{{ row.description || '这份技能没写一句话说它是干什么的' }}</span>
              <span class="ext__meta">正文 {{ row.body_chars }} 字 · {{ row.path }}</span>
              <!-- 读不过来的那一份也要能删掉：坏的东西清不掉比清得慢更糟。 -->
              <p v-if="row.error" class="ext__error">这一份读不过来：{{ row.error }}</p>
            </div>
            <button class="hud-btn" type="button" :disabled="busy" @click="flipSkill(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </button>
            <button v-if="pendingSkill !== row.slug" class="hud-btn" type="button" :disabled="busy" @click="pendingSkill = row.slug">
              删掉这一份
            </button>
            <template v-else>
              <span class="ext__warn">只删 <code>{{ skillDirOf(row) }}</code> 这一个目录，也就是 扩展/skills/{{ row.slug }}，不动你的资料、不动别的技能。</span>
              <button class="hud-btn danger" type="button" :disabled="busy" @click="removeSkill(row.slug)">确认删掉</button>
              <button class="hud-btn" type="button" :disabled="busy" @click="pendingSkill = ''">算了</button>
            </template>
          </li>
        </ul>

        <!-- ---------------------------------------------------- server -->
        <h3 class="hud-label ext__section">MCP 服务 · 登记 {{ state.servers.length }} 条</h3>
        <div class="ext__headrow">
          <span class="ext__meta">{{ mcpWords }}</span>
          <button class="hud-btn" type="button" :disabled="busy" @click="reconnect">重连</button>
          <button class="hud-btn" type="button" :disabled="busy" @click="showHttp = !showHttp">
            {{ showHttp ? '收起这条' : '加一条 http 服务' }}
          </button>
        </div>
        <p v-if="state.mcp.error" class="ext__error">{{ state.mcp.error }}</p>

        <div v-if="showHttp" class="ext__form ext__form--inner">
          <input v-model="httpName" class="hud-field ext__q" placeholder="名字，例如 my-weather" />
          <input v-model="httpUrl" class="hud-field ext__url" placeholder="https://… 的完整地址" />
          <button class="hud-btn hud-btn--primary" type="button" :disabled="busy || !httpName.trim() || !httpUrl.trim()" @click="addHttp">登记</button>
        </div>
        <p class="ext__hint">这一颗只能加 <strong>http(s)</strong> 的服务：它只是登记一个地址，不在本机起任何进程，风险和「让她上网查」同一档。会在本机起进程的 stdio 那类<strong>不在页面上加</strong> —— 那种只能从搜索结果装，好让那条命令行在装之前摊给你看过一遍。</p>

        <p v-if="!state.servers.length" class="ext__empty">还没有装过任何东西。到「搜索」那一格勾上「MCP server」搜一条，或者用上面那颗「加一条 http 服务」登记一个你自己的地址。</p>
        <ul v-else class="ext__list">
          <li v-for="row in state.servers" :key="row.name" class="ext__row" :class="{ 'ext__row--off': !row.enabled }">
            <span class="hud-chip">{{ row.transport }}</span>
            <div class="ext__row-main">
              <span class="ext__name">{{ row.name }}</span>
              <span class="ext__summary">{{ row.note || '这条没留下说明' }}</span>
              <span class="ext__meta" :class="{ 'ext__meta--bad': !!serverError(row) }">{{ serverWords(row) }}</span>
              <span v-if="row.command" class="ext__cmd ext__cmd--flat">{{ [row.command, ...row.args].join(' ') }}</span>
              <span v-else-if="row.url" class="ext__cmd ext__cmd--flat">{{ row.url }}</span>
              <span v-if="row.needs.length" class="ext__meta">要本机装：{{ row.needs.join('、') }}</span>
              <span v-if="row.env_from.length" class="ext__meta">要环境变量：{{ row.env_from.join('、') }}（密钥不落进扩展文件，页面上也不回显）</span>
              <span v-if="row.source" class="ext__meta">来自 {{ row.source }}</span>
              <p v-if="serverError(row)" class="ext__error">{{ serverError(row) }}</p>
            </div>
            <button class="hud-btn" type="button" :disabled="busy" @click="flipServer(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </button>
            <button v-if="pendingServer !== row.name" class="hud-btn" type="button" :disabled="busy" @click="pendingServer = row.name">
              删掉
            </button>
            <template v-else>
              <span class="ext__warn">删掉的是 {{ state.servers_file || 'servers.json' }} 里这一条登记，不碰盘上任何别的东西；它要是正在跑，要等下一次「重连」才停。</span>
              <button class="hud-btn danger" type="button" :disabled="busy" @click="removeServer(row.name)">确认删掉</button>
              <button class="hud-btn" type="button" :disabled="busy" @click="pendingServer = ''">算了</button>
            </template>
          </li>
        </ul>
      </template>

      <!-- ==================================================== 运行环境 -->
      <template v-else>
        <div class="ext__runtime">
          <div v-for="chip in runtimeChips" :key="chip.name" class="hud-chip" :class="chip.warn ? 'hud-chip--warn' : ''">
            <span class="hud-dot" :class="chip.warn ? 'warn' : 'listening'"></span>
            <span class="hud-label">{{ chip.words }}</span>
          </div>
        </div>
        <p class="ext__hint">这几颗说的是<strong>本机</strong>有没有那些命令，不是小夜缺不缺东西。缺 <code>npx</code> 时，npm 那一路的条目一律装不上 —— 装上了也是一条永远起不来的登记，所以那一颗按钮是灰的并写明为什么灰。补上 Node.js 再回来点「刷新」就行，不用重启小夜。</p>
        <p v-if="state.runtime.error" class="ext__error">{{ state.runtime.error }}</p>
        <!-- 每一样命令在本机哪个位置：「缺 npx」这句话能不能照着修，取决于这里有没有写出来。 -->
        <ul v-if="runtimePaths.length" class="ext__list">
          <li v-for="entry in runtimePaths" :key="entry.name" class="ext__row">
            <span class="ext__name">{{ entry.words }}</span>
            <span class="ext__cmd ext__cmd--flat ext__path">{{ entry.path }}</span>
          </li>
        </ul>

        <div class="ext__headrow">
          <span class="hud-label">这一层落在哪儿</span>
          <button class="hud-btn" type="button" :disabled="busy" @click="load">刷新</button>
        </div>
        <p class="ext__cmd">{{ state.directory || '（这个进程没接扩展这一层，没有目录）' }}</p>
        <p class="ext__cmd">{{ state.servers_file || '（这个进程没接扩展这一层，没有文件）' }}</p>
        <p class="ext__hint">这两条是给你自己去打开看的：装了什么、哪一条连不上，盘上都留得下来。技能在第一个目录的 <code>skills</code> 子目录里，每条 server 登记在第二个文件里。</p>

        <h3 class="hud-label ext__section">最近几笔扩展操作 · {{ state.log.length }} 条</h3>
        <p v-if="!state.log.length" class="ext__empty">还没有操作记录。这一格记的是这台机器上真动过的动作（装了什么、启停了哪一条、删了什么），不是她说过什么。</p>
        <ul v-else class="ext__list">
          <li v-for="(row, index) in state.log" :key="index" class="ext__row ext__row--log">
            <span class="hud-chip hud-num">{{ stamp(row.at) || '时间读不出' }}</span>
            <span class="ext__text">{{ row.event }}</span>
          </li>
        </ul>
      </template>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * 扩展市场：三格 —— 搜索、已装、运行环境。
 *
 * 结构照「助手」那张弹窗（遮罩点击关闭、标签行在头部、只在打开时读一次），不另造一套机制。
 * **没有轮询**：这一层的每一次改动都回一整份读回来的状态，界面照着它画。轮询会把「你刚
 * 按下去的那一下」擦掉，而这三个 tab 都是人特意打开来看的东西。
 *
 * 装这一条上刻意做重：后端只认「刚才搜出来的那批 id」，页面传不进命令行。所以没搜过的时候
 * 那颗「装上」是灰的并且写明为什么灰；点下去先出一张卡，MCP 那类把完整命令行摊开、技能那类
 * 把落盘目录摊开，第二颗按钮才真的动手。少做一步，这一页就等于一个命令输入框。
 */
import { computed, onMounted, ref, watch } from 'vue'
import {
  extensionsAddHttp,
  extensionsDetail,
  extensionsInstall,
  extensionsLayerSet,
  extensionsReconnect,
  extensionsRecommended,
  extensionsRemoveItem,
  extensionsSearch,
  extensionsServerRemove,
  extensionsServerSet,
  extensionsState,
  extensionsUpdate,
  skillRemove,
  skillSet,
  type ExtensionCatalog,
  type ExtensionDetail,
  type ExtensionSearch,
  type ExtensionServer,
  type ExtensionSkill,
  type ExtensionState,
} from '@/api/bridge'
import { stamp } from '@/api/time'

type TabId = 'search' | 'installed' | 'runtime'

const TABS: readonly { id: TabId; label: string }[] = [
  { id: 'search', label: '搜索' },
  { id: 'installed', label: '已装' },
  { id: 'runtime', label: '运行环境' },
]

/**
 * 后端 ``runtime.labels`` 里没有那一个键的回退写法。
 *
 * ``detect()`` 会把小夜自己那个解释器以 ``this-runtime`` 塞进 ``present``，而它不在标签表里 ——
 * 照原样念出来就是一行「this-runtime」，用户读不懂那是什么。
 */
const EXTRA_LABELS: Record<string, string> = {
  'this-runtime': '小夜自己这个解释器（随包那个）',
}

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const tab = ref<TabId>('search')
const notice = ref('')
const noticeBad = ref(false)
const busy = ref(false)

/**
 * 初始值是「还没读到」而不是「读到的是空的」。
 *
 * 全空形状和后端那句「这个进程没有接扩展这一层」在屏上必须是两回事，所以另记一个
 * ``loaded`` 把它说清楚 —— 同一笔账她自学那一格已经付过一次了。
 */
const state = ref<ExtensionState>({
  directory: '',
  servers_file: '',
  layer_enabled: false,
  servers: [],
  skills: [],
  mcp: { enabled: false, running: false, servers: 0, connected: 0, tools: 0, statuses: [], errors: {}, error: '' },
  runtime: { present: [], paths: {}, labels: {}, missing: [], error: '' },
  search: { query: '', items: [], sources: [], failures: [] },
  store_error: '',
  log: [],
  verdicts: {},
  error: '',
})
const loaded = ref(false)

const query = ref('')
const wantSkill = ref(true)
const wantMcp = ref(true)
const curatedOnly = ref(false)
const searching = ref(false)
/** 这一屏有没有真搜过一次。没搜过就不许装 —— 后端也是这条规矩。 */
const searched = ref(false)
const search = ref<ExtensionSearch>({ query: '', items: [], sources: [], failures: [], error: '' })
const pending = ref<ExtensionCatalog | null>(null)
const installing = ref(false)
const installResult = ref<ExtensionState['install'] | null>(null)

/** 推荐那一格：没搜过的时候屏上摆的就是它。``error`` 非空 = 这份清单没读到。 */
const recommended = ref({ items: [] as ExtensionCatalog[], error: '', at: 0, where: '' })
/** 打开着的详情卡。null = 没开。 */
const detail = ref<ExtensionDetail | null>(null)
/** 哪一条正在等"确认删掉"那一下。跨页签会被清掉，和确认卡同一条规矩。 */
const pendingRemove = ref('')
const updating = ref(false)
/** 加载失败的头像地址：记下来就退成首字母方块，不让屏上出现一个破图框。 */
const brokenIcons = ref<string[]>([])

const httpName = ref('')
const httpUrl = ref('')
const showHttp = ref(false)
const pendingSkill = ref('')
const pendingServer = ref('')

const nothingChecked = computed(() => !wantSkill.value && !wantMcp.value)

const kindsHint = computed(() =>
  nothingChecked.value
    ? '两样都没勾，那就没有东西可搜 —— 先勾一样。'
    : '勾哪几样就只问哪几路。三路是并着问的，一路没通不影响其余两路出来。',
)

/** 按来源分组：来源那一行自己就是「命中几条、花多少毫秒、有没有失败」。 */
const groups = computed(() => {
  const items = curatedOnly.value
    ? search.value.items.filter((row) => row.source === 'curated')
    : search.value.items
  const rows = search.value.sources.map((source) => {
    const found = items.filter((row) => row.source === source.source)
    return {
      source: source.source,
      label: source.label || source.source,
      // 命中数从条目里数出来，不照抄后端的 ``hits``：这样「报 3 条、屏上一行都没有」不可能出现。
      hits: found.length,
      milliseconds: source.milliseconds,
      error: source.error,
      items: found,
    }
  })
  // 兜一组「后端没把它算作一路来源、却确实给了条目」的东西。不兜就是条目悄悄消失，
  // 而这一层反复付过账的正是这种「结果是完整的，画出来却少一块」。
  const known = new Set(search.value.sources.map((source) => source.source))
  const orphans = items.filter((row) => !known.has(row.source))
  if (orphans.length) {
    rows.push({
      source: '__other',
      label: '没归路的来源',
      hits: orphans.length,
      milliseconds: 0,
      error: '',
      items: orphans,
    })
  }
  return rows
})

type Panel = {
  source: string
  label: string
  hits: number
  milliseconds: number
  error: string
  items: ExtensionCatalog[]
}

/**
 * 一屏上摆的"分组"。没搜过 = 推荐那一条；搜过 = 三路各自一组。
 *
 * 合成一个计算属性是为了**只有一份行模板**：两份的话，改了图标改了按钮，
 * 另一份会安静地留在旧样子 —— 而屏上看着两处都有功能，点下去才发现不一样。
 */
const panels = computed<Panel[]>(() => {
  if (searched.value) return groups.value
  return [
    {
      source: 'recommended',
      label: '推荐 · 作者逐条验过的那份',
      hits: recommended.value.items.length,
      milliseconds: 0,
      error: recommended.value.error ? `这份清单没读到：${recommended.value.error}` : '',
      items: recommended.value.items,
    },
  ]
})

/** 详情卡上那几块读的是 ``detail.item``；没读到就是一份全空，界面上那些句子会退成"没写"。 */
const detailOf = computed<ExtensionCatalog>(
  () =>
    (detail.value?.item ?? {}) as ExtensionCatalog,
)

const mcpWords = computed(() => {
  const mcp = state.value.mcp
  const total = state.value.servers.length
  const count = mcpCount(mcp.servers)
  const parts = [`这一层登记的 ${total} 条`]
  parts.push(count === null ? '连着几台没读数' : `MCP 那边报的是 ${count} 台`)
  parts.push(mcp.running ? 'MCP 现在在跑' : 'MCP 现在没在跑')
  parts.push(typeof mcp.tools === 'number' ? `工具共 ${mcp.tools} 个` : '工具数没读数')
  return parts.join(' · ')
})

/**
 * 运行环境那一格：present 用「有」，missing 用告警色。
 *
 * 两样都要画出来 —— 只画 present 的话，「这台机器没有 uvx」在屏上什么都不显示，
 * 而它正是那条条目装不上的原因。
 */
const runtimeChips = computed(() => {
  const runtime = state.value.runtime
  const words = (name: string): string => runtime.labels[name] ?? EXTRA_LABELS[name] ?? name
  const present = runtime.present.map((name) => ({ name, words: words(name), warn: false }))
  const missing = runtime.missing.map((name) => ({ name, words: `没有 ${words(name)}`, warn: true }))
  return [...present, ...missing]
})

/**
 * 每一样命令在本机的哪一个位置。
 *
 * 摊开路径不是给程序员看的装饰：「缺 npx」这句话有没有可操作性，取决于你能不能拿这一行去问
 * 别人「那我该装什么」；而 ``which`` 找到的那一条也正是「本机装了两个 Node，用的是哪一个」唯一的答案。
 */
const runtimePaths = computed(() => {
  const runtime = state.value.runtime
  return Object.entries(runtime.paths)
    .map(([name, path]) => ({ name, words: runtime.labels[name] ?? EXTRA_LABELS[name] ?? name, path }))
    .sort((one, two) => one.words.localeCompare(two.words))
})

/** 技能那一行落在哪个目录。后端给了就用它的，没给（读不过来的那一份）就照同一条规矩拼出来。 */
function skillDirOf(row: ExtensionSkill): string {
  if (row.path) return row.path
  return `${state.value.directory || '扩展'}/skills/${row.slug}`
}

function kindWord(kind: string): string {
  if (kind === 'skill') return '技能'
  if (kind === 'mcp') return 'MCP'
  return kind
}

function needsWords(item: ExtensionCatalog): string {
  return item.needs.length ? item.needs.join('、') : '没有特别要求'
}

function say(message: string, bad = false): void {
  notice.value = message
  noticeBad.value = bad
}

/**
 * 这颗「装上」现在是灰的，原因写在这句话里。
 *
 * 两个原因都是真的、都必须说出来，而不是让按钮自己看起来坏了：
 * 1）这一屏没搜过 —— 后端只认刚才搜出来的 id，这是它有意拒绝的形状（否则这一页就成了命令输入框）；
 * 2）这一条要的本机命令不在 —— 装上一条永远起不来的登记，就是本项目反复要求停止生产的那种假能力。
 */
/**
 * 这颗「装上」现在能不能按，以及为什么不能。
 *
 * 判断**读后端随 state 一起给的那份判词**（``verdicts``），页面不再自己数 needs：
 * 列表和详情两处各算一遍的话，同一个东西在两个位置上会说出两句话 —— 这一层为这种
 * "看起来都正常、就是不一致"付过不止一次账。
 */
function installBlock(item: ExtensionCatalog): string {
  const verdict = state.value.verdicts[item.id]
  if (verdict) {
    if (verdict.installed.yes) return ''
    return verdict.install.can ? '' : verdict.install.words
  }
  const gaps = item.needs.filter((need) => !state.value.runtime.present.includes(need))
  if (gaps.length) return `这台机器上缺 ${gaps.join('、')}，装上了也起不来。`
  return ''
}

/** 这一条在盘上装了没有（判词没读到时按没装处理 —— 那时按钮是「装上」，点了会要你先刷新）。 */
function isInstalled(item: ExtensionCatalog): boolean {
  return Boolean(state.value.verdicts[item.id]?.installed.yes)
}

function installedWords(item: ExtensionCatalog): string {
  return state.value.verdicts[item.id]?.installed.words ?? ''
}

function canUpdate(item: ExtensionCatalog): boolean {
  return Boolean(state.value.verdicts[item.id]?.update.can)
}

function updateWhy(item: ExtensionCatalog): string {
  return state.value.verdicts[item.id]?.update.words ?? '这一条的账还没读到：按上面「刷新」再看一次。'
}

/** 「更新」按不了的时候，那句话要出现在行上 —— 不是只有鼠标悬停才看得见。 */
function updateBlock(item: ExtensionCatalog): string {
  if (!isInstalled(item) || canUpdate(item)) return ''
  return `这条更新不了：${updateWhy(item)}`
}

function removeWords(item: ExtensionCatalog): string {
  const verdict = state.value.verdicts[item.id]
  const where = verdict?.installed.path || ''
  if (item.kind === 'mcp') {
    return `删掉的是 ${where || 'servers.json'} 里这一条登记，不碰盘上任何别的东西；它要是正在跑，要等下一次「重连」才停。`
  }
  return `只删 ${where || `${item.name} 那个目录`} 这一个目录，不动你的资料、不动别的技能。`
}

/** 图标：只有 https 的头像地址才去加载（后端已经过白名单，这里只是分辨 emoji 和 URL）。 */
function pictureOf(item: ExtensionCatalog): string {
  const icon = String(item.icon || '')
  if (!icon.startsWith('https://')) return ''
  return brokenIcons.value.includes(icon) ? '' : icon
}

function markBroken(item: ExtensionCatalog): void {
  const icon = String(item.icon || '')
  if (icon && !brokenIcons.value.includes(icon)) brokenIcons.value = [...brokenIcons.value, icon]
}

/** 首字母方块：拿名字最后一段的第一个字。CJK 原样，拉丁字母转大写。 */
function monogram(name: string): string {
  const clean = String(name || '')
    .replace(/^@/, '')
    .split(/[\\/]+/)
    .filter(Boolean)
    .pop()
  const first = clean ? Array.from(clean)[0] : ''
  if (!first) return '？'
  return /[a-z]/i.test(first) ? first.toUpperCase() : first
}

/** 这一格的图是哪来的，说清楚 —— 拿不到图标不装成有。 */
function iconWords(item: ExtensionCatalog): string {
  if (pictureOf(item)) return '这一格的图标是来源给的头地址。'
  if (String(item.icon || '')) return '这一格的图标是清单里给的一个符号。'
  return '来源没给图标，这一格是名字首字母生成的。'
}

async function openDetail(item: ExtensionCatalog): Promise<void> {
  busy.value = true
  try {
    detail.value = await extensionsDetail(item.id)
    pendingRemove.value = ''
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

function closeDetail(): void {
  detail.value = null
  pendingRemove.value = ''
}

async function doUpdate(item: ExtensionCatalog): Promise<void> {
  if (updating.value || busy.value) return
  updating.value = true
  notice.value = ''
  try {
    const next = await extensionsUpdate(item.id)
    apply(next)
    if (next.error) say(next.error, true)
    else say(next.update?.detail || next.update?.reason || '更新这一步没回话')
    if (detail.value) detail.value = await extensionsDetail(item.id)
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    updating.value = false
  }
}

async function confirmRemove(item: ExtensionCatalog): Promise<void> {
  if (busy.value) return
  busy.value = true
  pendingRemove.value = ''
  try {
    const next = await extensionsRemoveItem(item.id, true)
    apply(next)
    if (next.error) say(next.error, true)
    else say(next.removed?.detail || `删掉了这一条：${item.name}`)
    if (detail.value) detail.value = await extensionsDetail(item.id)
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function refreshRecommended(): Promise<void> {
  try {
    recommended.value = await extensionsRecommended()
  } catch (err) {
    recommended.value = {
      items: [],
      error: err instanceof Error ? err.message : String(err),
      at: 0,
      where: '',
    }
  }
}

/** MCP 那一条要在装之前摊开的完整命令行：这就是这一层唯一的危险面。 */
function commandLine(item: ExtensionCatalog): string {
  const command = String(item.install['command'] ?? '')
  const args = Array.isArray(item.install['args']) ? item.install['args'].map(String) : []
  if (command) return [command, ...args].join(' ')
  const url = String(item.install['url'] ?? '')
  return url || '这一条没写明要跑什么，后端会拒绝装它'
}

/** 技能那一条要落进哪个目录。目录名后端消毒过，这里只是给人先看一眼。 */
function skillTarget(item: ExtensionCatalog): string {
  const raw = String(item.install['path'] ?? item.name)
  const slug = raw.replace(/\\/g, '/').split('/').filter(Boolean).pop() ?? 'skill'
  return `${state.value.directory || '（这一层没报出目录）'}/skills/${slug}`
}

/**
 * ``mcp.servers`` 是条数，逐台的情况在 ``mcp.statuses``（后端 ``McpService.stats()``）。
 *
 * 两种形状都还容错读一遍：老预览/老后端可能把 ``servers`` 给成数组。对数字直接取
 * ``.length`` 会得到 undefined，那一格就悄悄少一行 —— 少的那一行说的正是「连上了几个」。
 */
/** 某一台 server 的逐条状态。`statuses` 是现在的事实，`servers` 数组留给老负载。 */
function mcpStatus(name: string): { connected: boolean; tool_count: number; error: string } | null {
  const rows = [...mcpRows(state.value.mcp.statuses), ...mcpRows(state.value.mcp.servers)]
  const found = rows.find((one) => String(one['name'] ?? '') === name)
  if (!found) return null
  return {
    connected: Boolean(found['connected']),
    tool_count: typeof found['tool_count'] === 'number' ? found['tool_count'] : 0,
    error: String(found['error'] ?? ''),
  }
}

function mcpCount(value: unknown): number | null {
  if (typeof value === 'number') return value
  if (Array.isArray(value)) return value.length
  return null
}

/** 数组那一支如果真是每台的读数，就顺手用来取每台的工具数；今天后端回的是数字，所以是空表。 */
function mcpRows(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((row): row is Record<string, unknown> => !!row && typeof row === 'object')
    : []
}

function serverError(row: ExtensionServer): string {
  return state.value.mcp.errors[row.name] ?? ''
}

/**
 * 这一条现在连上没有，一句话。
 *
 * **失败原文不写在这里** —— 它紧跟着单独一行 ``.ext__error`` 画出来。同一句话在同一条上写两遍，
 * 用户会以为是两个不同的错，而这里要的恰恰是「一眼看清是哪一个错」。
 */
function serverWords(row: ExtensionServer): string {
  if (serverError(row)) return '没连上，原因就在下面那一句'
  if (!state.value.layer_enabled) return '总开关关着，所以这一条没连'
  if (!row.enabled) return '这一条被停用了，没连'
  if (!state.value.mcp.running) return 'MCP 现在没在跑，所以没连'
  const row0 = mcpStatus(row.name)
  if (row0 === null) return '连着（后端还没回这一台的逐条状态，总数看上面那一行）'
  return row0.connected ? `连着 · ${row0.tool_count} 个工具` : `没连上：${row0.error || '后端没给原因'}`
}

function select(next: TabId): void {
  tab.value = next
  // 确认卡与确认条不许跨页签活着：在「搜索」里摊开的那条命令行，翻出去再翻回来还挂在屏上，
  // 下一次随手一点就真装了一个进程 —— 和「她」那一格的确认条是同一个道理。
  pending.value = null
  pendingSkill.value = ''
  pendingServer.value = ''
  // 详情卡和"等确认删"也不许跨页签活着：翻出去再翻回来还挂着一张摊开的命令行，
  // 下一次随手一点就真装了一个进程。
  detail.value = null
  pendingRemove.value = ''
  if (next === 'search' && !searched.value) void refreshRecommended()
}

function apply(next: ExtensionState): void {
  state.value = next
  loaded.value = true
  if (next.error) say(next.error, true)
  if (next.search.items.length) {
    search.value = {
      query: next.search.query,
      items: next.search.items,
      sources: next.search.sources,
      failures: next.search.failures,
      error: '',
    }
  }
}

async function load(): Promise<void> {
  busy.value = true
  if (!searched.value) void refreshRecommended()
  try {
    apply(await extensionsState())
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function runSearch(): Promise<void> {
  if (searching.value || nothingChecked.value) return
  searching.value = true
  notice.value = ''
  detail.value = null
  pendingRemove.value = ''
  // 搜一次就是一次新的授权范围：上一张摊开的确认卡必须跟着作废，不能留着让人点「确认装上」
  // 装一条已经不在结果里的东西。
  pending.value = null
  installResult.value = null
  try {
    const kinds = [wantSkill.value ? 'skill' : '', wantMcp.value ? 'mcp' : ''].filter(Boolean)
    const answer = await extensionsSearch(query.value, kinds)
    search.value = answer
    searched.value = true
    if (answer.error) say(answer.error, true)
    else if (!answer.items.length && !answer.failures.length) say(`「${answer.query || query.value}」：三路都问到了，市场里没有对得上的条目。`)
  } catch (err) {
    searched.value = false
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    searching.value = false
  }
}

function askInstall(item: ExtensionCatalog): void {
  installResult.value = null
  notice.value = ''
  pending.value = item
}

async function confirmInstall(): Promise<void> {
  const item = pending.value
  if (!item || installing.value) return
  installing.value = true
  try {
    const next = await extensionsInstall(item.id)
    apply(next)
    installResult.value = next.install ?? null
    if (next.error) say(next.error, true)
    else if (next.install && !next.install.ok) say(next.install.reason || '没装上，原因见上面那一句', true)
    else say(next.install?.detail || '装上了')
    if (detail.value) detail.value = await extensionsDetail(item.id)
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    pending.value = null
    installing.value = false
  }
}

/** 总开关。按下去之后画的是后端**读回来**的那一份，不是「我刚点了什么」。 */
async function flipLayer(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    apply(await extensionsLayerSet(!state.value.layer_enabled))
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function flipSkill(row: ExtensionSkill): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    apply(await skillSet(row.slug, !row.enabled))
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function removeSkill(slug: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  pendingSkill.value = ''
  try {
    const next = await skillRemove(slug, true)
    apply(next)
    say(next.error ? next.error : `删掉了这一份：${slug}`, !!next.error)
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function flipServer(row: ExtensionServer): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    apply(await extensionsServerSet(row.name, !row.enabled))
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function removeServer(name: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  pendingServer.value = ''
  try {
    const next = await extensionsServerRemove(name, true)
    apply(next)
    say(next.error ? next.error : `删掉了这条登记：${name}`, !!next.error)
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function reconnect(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    apply(await extensionsReconnect())
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

async function addHttp(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    const next = await extensionsAddHttp(httpName.value.trim(), httpUrl.value.trim())
    apply(next)
    if (!next.error) {
      httpName.value = ''
      httpUrl.value = ''
      showHttp.value = false
    }
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  } finally {
    busy.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) void load()
  },
)

onMounted(() => {
  if (props.open) void load()
})
</script>

<style scoped>
/*
 * 46：比设置弹窗（40）和助手弹窗（44）都高，且这张是要从设置里也开得动的入口之一。
 * 这窗口为「看得见但够不着」付过一次账 —— 同一个 z-index 层里靠文档顺序决胜，
 * 所以光有高号不够，App.vue 里那张也必须挂在设置之后。
 */
.ext {
  position: fixed;
  inset: 0;
  z-index: 46;
  display: grid;
  place-items: center;
}

.ext__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.ext__box {
  position: relative;
  width: min(920px, 92vw);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  padding: 12px 14px 16px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: linear-gradient(rgba(9, 22, 38, 0.985), rgba(3, 7, 13, 0.99));
  box-shadow: var(--hud-elev-2);
}

.ext__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.ext__tabs,
.ext__headrow,
.ext__form,
.ext__kinds,
.ext__confirm-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.ext__headrow {
  justify-content: space-between;
  margin: 6px 0;
}

.ext__form {
  margin: 8px 0;
}

.ext__form--inner {
  margin: 6px 0;
}

.ext__q {
  flex: 1 1 240px;
  min-width: 0;
}

.ext__url {
  flex: 1 1 300px;
  min-width: 0;
}

.ext__kind {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: var(--hud-control-h);
  padding: 0 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-pill);
  background: rgba(77, 216, 255, 0.04);
  cursor: pointer;
}

.ext__kind input {
  accent-color: var(--hud-cyan);
  margin: 0;
}

.ext__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.ext__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 6px 0 0;
  padding: 0;
  list-style: none;
}

/*
 * 一行里左边是「给人读的一段」，右边是那几颗 26px 的控件。``min-width: 0`` 不是装饰：
 * 不给它下限，一条长命令行会把整行撑开、把按钮顶出弹窗（这个项目量过一次）。
 */
.ext__row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: var(--hud-surface);
  color: var(--hud-text);
  font-size: 12px;
}

.ext__row--off {
  opacity: 0.55;
}

.ext__row--log {
  align-items: center;
  border-radius: var(--hud-pill);
  padding: 4px 10px;
}

.ext__row-main {
  flex: 1 1 220px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.ext__name {
  color: var(--hud-cyan);
}

.ext__summary,
.ext__text {
  color: var(--hud-text);
  word-break: break-word;
}

.ext__meta {
  color: var(--hud-dim);
  font-size: 11px;
}

.ext__meta--bad {
  color: var(--hud-red);
}

.ext__link {
  color: var(--hud-cyan);
  text-decoration: none;
}

.ext__group {
  margin: 10px 0 0;
}

.ext__group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-bottom: 3px;
  border-bottom: 1px solid var(--hud-line);
}

.ext__group-name {
  color: var(--hud-cyan);
}

.ext__note,
.ext__warn,
.ext__empty,
.ext__hint,
.ext__edge {
  margin: 6px 0;
  font-size: 12px;
}

.ext__note {
  color: var(--hud-cyan);
}

.ext__note--bad {
  color: var(--hud-red);
}

.ext__warn {
  color: var(--hud-amber);
}

/* 后端回来的每一句失败原文都走这一条：红字、可拖选、不许咽掉、不许折成「没有结果」。 */
.ext__error {
  margin: 5px 0;
  color: var(--hud-red);
  font-size: 12px;
  word-break: break-word;
}

.ext__hint,
.ext__empty,
.ext__edge {
  color: var(--hud-dim);
}

.ext__edge {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
}

.ext__layer,
.ext__confirm {
  margin: 4px 0 10px;
  padding: 8px 10px;
  border: 1px solid var(--hud-edge);
  border-radius: var(--hud-radius);
  background: var(--hud-surface-2);
}

/* 装之前那张卡要比别的块更抢眼：它是这一页唯一一次「你要允许本机跑一个东西」。 */
.ext__confirm {
  border-color: rgba(255, 181, 71, 0.5);
  background: rgba(255, 181, 71, 0.06);
}

.ext__confirm-title {
  margin: 0 0 4px;
  color: var(--hud-text);
  font-size: 13px;
}

.ext__cmd {
  margin: 4px 0;
  padding: 5px 8px;
  border: 1px solid var(--hud-line);
  border-radius: 8px;
  background: rgba(3, 9, 16, 0.8);
  font-family: var(--hud-mono);
  font-size: 12px;
  color: var(--hud-text);
  word-break: break-all;
}

.ext__cmd--flat {
  margin: 0;
  padding: 0;
  border: none;
  background: none;
  color: var(--hud-dim);
}

/* 路径和标签并排在同一行时，是路径那一侧要能让位：不给下限它就撑开整行、把别的顶出弹窗。 */
.ext__path {
  flex: 1 1 200px;
  min-width: 0;
}

.ext__section {
  margin: 14px 0 4px;
  padding-bottom: 3px;
  border-bottom: 1px solid var(--hud-line);
  color: var(--hud-cyan);
}

.ext__runtime {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 6px 0;
}

/*
 * 图标那一格：emoji 和头像地址共用一个盒子，换来源不换布局。
 * 它是颗按钮（不是张死图）—— 点它就是开详情，所以得看起来点得动。
 */
.ext__icon {
  flex: 0 0 auto;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 1px solid var(--hud-line);
  border-radius: 9px;
  background: rgba(77, 216, 255, 0.07);
  color: var(--hud-cyan);
  font-family: var(--hud-mono);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  overflow: hidden;
}

.ext__icon img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.ext__icon--big {
  width: 44px;
  height: 44px;
  font-size: 19px;
  /* 详情卡上这一格只是摆出来看，再点一次还是这张卡 */
  cursor: default;
}

.ext__icon-mono {
  letter-spacing: 0;
}

/* 名字也是一颗开详情的按钮：只给图标一个入口，一半人不会去点那个小方块。 */
.ext__name--link {
  padding: 0;
  border: none;
  background: none;
  color: var(--hud-cyan);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.ext__detail {
  position: relative;
  margin: 8px 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--hud-edge);
  border-radius: var(--hud-radius);
  background: linear-gradient(rgba(11, 26, 44, 0.96), rgba(4, 10, 18, 0.97));
  box-shadow: var(--hud-elev-1);
}

.ext__detail-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ext__detail-titles {
  flex: 1 1 180px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
</style>
