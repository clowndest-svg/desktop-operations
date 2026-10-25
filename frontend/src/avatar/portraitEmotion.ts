export type PortraitEmotion = 'neutral' | 'happy' | 'pout' | 'soft'

/** Expressions reflect the assistant's words, never a guess about the user's mood. */
export function replyEmotion(text: string): PortraitEmotion {
  const recent = text.slice(-700).replace(/```[\s\S]*?```/g, '')
  if (/难过|悲伤|去世|哀悼|抑郁|伤心|很抱歉|对不起|sorry|grief/i.test(recent)) return 'soft'
  const unnegated = recent.replace(/(?:别|不要|没有|并不|不必|不会|不再|不)(?:再|那么)?生气/g, '')
  if (/我.{0,3}生气|气鼓鼓|哼[！!，,～~]|😤|嘟嘴/.test(unnegated)) return 'pout'
  if (/太好了|哈哈|好开心|很开心|很高兴|恭喜|祝你|好耶|😊|😄|🥰|happy to|congratulations/i.test(recent)) return 'happy'
  return 'neutral'
}
