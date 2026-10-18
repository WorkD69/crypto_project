() => [...document.querySelectorAll('#MiddleColumn .Message[data-message-id]')].map(el => {
  const content = el.querySelector('.text-content');
  const copy = content?.cloneNode(true);
  copy?.querySelectorAll('.MessageMeta, .Reactions, [data-ignore-on-paste]').forEach(n => n.remove());
  copy?.querySelectorAll('br').forEach(n => n.replaceWith(document.createTextNode('\n')));
  const time = el.querySelector('.message-time');
  const album = el.querySelector('[data-grouped-id]');
  const marker = el.querySelector('.bottom-marker[data-album-main-id]');
  const media = [...el.querySelectorAll('.media-inner')].map((m, position) => ({
    position, kind: m.querySelector('video, .message-media-duration') ? 'video' : 'photo',
    observed_id: m.getAttribute('data-message-id') || null,
    label: m.getAttribute('aria-label') || null,
    state: 'PENDING', path: null,
    dom_album_main_message_id: marker?.getAttribute('data-album-main-id') || null,
    dom_album_last_message_id: marker?.getAttribute('data-last-message-id') || null,
    relation: 'same_visible_message_container'
  }));
  return {
    message_id: el.getAttribute('data-message-id'),
    text: copy?.textContent?.trim() || '',
    original_time_raw: time?.getAttribute('title') || time?.textContent || null,
    album_id: el.getAttribute('data-grouped-id') || album?.getAttribute('data-grouped-id') || null,
    media, warnings: []
  };
})
