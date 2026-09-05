/* Executed inside the active note page; returns plain, serializable data only. */
const expectedId = arguments[0];
const text = node => (node?.innerText || node?.textContent || '').trim();
const pickText = (root, selectors) => {
  for (const selector of selectors) {
    const value = text(root.querySelector(selector));
    if (value) return value;
  }
  return '';
};
const mediaUrl = value => {
  if (typeof value !== 'string') return '';
  if (value.startsWith('//')) value = 'https:' + value;
  return /^https?:\/\//i.test(value) ? value : '';
};
const imageUrl = image => {
  if (typeof image === 'string') return mediaUrl(image);
  if (!image) return '';
  const info = image.infoList || image.info_list || [];
  return mediaUrl(image.urlDefault || image.url_default ||
    info.find(item => /^(WB_DFT|CRD_WM_JPG)$/.test(item.imageScene || item.image_scene || ''))?.url ||
    image.url || info.find(item => item.url)?.url || image.urlPre || image.url_pre);
};
const unique = values => [...new Set(values.filter(Boolean))];
const root = document.querySelector('#noteContainer, .note-detail, .note-detail-container');
const state = window.__INITIAL_STATE__;
const noteState = state?.note;
const map = noteState?.noteDetailMap || noteState?.note_detail_map || {};
const wanted = expectedId || noteState?.currentNoteId;
const entry = wanted ? map[wanted] : null;
const note = entry?.note || entry?.data?.note;

// A noteDetailMap entry must match this navigation; never save a feed card or
// another previously opened note when a login/error page is being displayed.
if (note && (!expectedId || String(note.noteId || note.note_id || wanted).toLowerCase() === expectedId.toLowerCase())) {
  const images = unique((note.imageList || note.image_list || []).map(imageUrl));
  if (!images.length && (note.type === 'video' || note.video)) {
    const cover = imageUrl(note.cover || note.video?.cover || note.video?.image);
    if (cover) images.push(cover);
  }
  const title = String(note.title || note.displayTitle || '').trim();
  const content = String(note.desc || note.description || '').trim();
  const author = note.user || note.author || {};
  if (title || content || images.length || note.video) {
    return {
      ready: true, note_id: String(note.noteId || note.note_id || wanted),
      title, content, author: String(author.nickname || author.nickName || author.name || ''),
      published_at: note.time || note.publishTime || note.publish_time || '',
      location: note.ipLocation || note.ip_location || '',
      type: note.type === 'video' || note.video ? 'video' : (images.length ? 'image' : 'text'),
      images, source: 'initial_state'
    };
  }
}

const errorRoot = document.querySelector('.error-wrapper, .error-page, .not-found, .note-not-found');
const errorText = text(errorRoot || (!root ? document.body : null));
const unavailable = errorText.match(/笔记不存在|笔记已删除|该笔记已被删除|当前内容无法展示|这篇笔记暂时无法浏览|内容已删除|笔记暂时无法访问/);
if (unavailable) return {unavailable: unavailable[0]};
if (!root) return null;

const meta = name => document.querySelector(`meta[property="${name}"], meta[name="${name}"]`)?.content || '';
const detailTitle = pickText(root, ['#detail-title', '.note-content .title', '.note-title']);
const content = pickText(root, ['#detail-desc', '.note-content .desc', '.note-text']);
const images = [];
for (const element of root.querySelectorAll('.note-slider img, .swiper-slide img, .media-container img, .image-container img, .live-photo img')) {
  const src = mediaUrl(element.currentSrc || element.getAttribute('src') || element.getAttribute('data-src'));
  if (src) images.push(src);
}
let video = root.querySelector('video, xg-video-container, xg-poster');
for (const element of root.querySelectorAll('video[poster], xg-poster, .xgplayer-poster')) {
  const poster = mediaUrl(element.getAttribute('poster'));
  const background = getComputedStyle(element).backgroundImage.match(/url\(["']?(.*?)["']?\)/)?.[1];
  if (poster || mediaUrl(background)) images.push(poster || mediaUrl(background));
}
// Metadata may supplement a mounted detail view, but cannot establish one.
if (!detailTitle && !content && !images.length && !video) return null;
if (!images.length) {
  const cover = mediaUrl(meta('og:image'));
  if (cover) images.push(cover);
}
const rawDate = pickText(root, ['.bottom-container .date', '.note-content .date', '.publish-time']);
let published = rawDate, location = '';
const locationMatch = rawDate.match(/^(.*?)\s+(?:IP属地[：:]?\s*)?([^\s\d:]+)$/);
if (locationMatch && /\d|刚刚|昨天|前天/.test(locationMatch[1])) {
  published = locationMatch[1];
  location = locationMatch[2];
}
published = published.replace(/^编辑于\s*/, '');
return {
  ready: true, note_id: expectedId || '', title: detailTitle || meta('og:title').replace(/\s*[-|]\s*小红书.*$/, ''),
  content, author: pickText(root, ['.author-container .username', '.author-container .name', '.author-container .nickname', '.author-container .info a span', '.author-name']),
  published_at: published || meta('article:published_time'), location,
  type: video ? 'video' : (images.length ? 'image' : 'text'), images: unique(images), source: 'dom'
};
