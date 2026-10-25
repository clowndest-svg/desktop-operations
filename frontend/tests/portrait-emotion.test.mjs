import test from 'node:test'
import assert from 'node:assert/strict'
import { replyEmotion } from '../src/avatar/portraitEmotion.ts'
test('expressions follow the assistant reply conservatively',()=>{
  assert.equal(replyEmotion('太好了，恭喜你完成！😊'),'happy')
  assert.equal(replyEmotion('哼，我有点生气了，气鼓鼓。'),'pout')
  assert.equal(replyEmotion('别生气，我们一起解决这个问题。'),'neutral')
  assert.equal(replyEmotion('我并不生气。'),'neutral')
  assert.equal(replyEmotion('很抱歉听到你伤心的消息，祝你早日恢复。'),'soft')
  assert.equal(replyEmotion('文件已经保存到指定目录。'),'neutral')
})
