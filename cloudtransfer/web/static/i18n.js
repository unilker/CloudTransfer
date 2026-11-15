/* Interface text, in English and Turkish.
 *
 * Loaded before app.js so t() exists by the time anything renders. The page
 * ships with English in the markup, so it still reads correctly if this file
 * fails to load; the translation pass replaces it.
 *
 * Keys ending in _html carry markup (links, <b>, <code>) and are written to
 * innerHTML. They are our own strings, never anything a user supplied.
 */
'use strict';

const TRANSLATIONS = {
  en: {
    'lang.switch': 'Türkçe',
    'lang.switch.title': 'Switch the interface to Turkish',

    'app.tagline': 'Google Drive and OneDrive, either way, streamed in {size} MiB chunks. Nothing is written to disk.',
    'app.meta': 'v{version} · {parallel} files in parallel',

    // -- step 1
    'setup.heading': 'Set up access',
    'setup.checking': 'checking…',
    'setup.show': 'Show',
    'setup.hide': 'Hide',
    'setup.pill.done': 'done',
    'setup.pill.needed': 'needed',
    'setup.lede_html': 'Google and Microsoft do not let any program sign in with your email address and password &mdash; that was turned off for security, and their file APIs only accept an authorisation token. So each needs an application registered in your own account. It is free, takes a few minutes, and is <strong>only ever done once</strong>.',
    'setup.state.notSetUp': 'not set up',
    'setup.state.ready': 'ready',
    'setup.state.env': 'set by environment',

    'google.step1_html': 'Open the <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">Google Cloud Console</a> and create a project.',
    'google.step2_html': '<em>APIs &amp; Services &rarr; Library</em> &rarr; enable <b>Google Drive API</b>.',
    'google.step3_html': '<em>OAuth consent screen</em> &rarr; External &rarr; add your own address as a test user.',
    'google.step4_html': '<em>Credentials &rarr; Create credentials &rarr; OAuth client ID</em> &rarr; <b>Desktop app</b>.',
    'google.step5_html': 'Google offers a <b>Download JSON</b> button. Save that file, then choose it below &mdash; nothing needs copying by hand.',

    'import.label': 'The JSON file Google gave you',
    'import.button': 'Choose file…',
    'import.hint': 'Usually called client_secret_….json, in your Downloads folder. It fills both boxes below for you.',
    'import.ok': 'Read from {name}. Now press Save.',
    'import.bad': 'That file does not look like a Google OAuth client. Download it again from Credentials.',
    'import.unreadable': 'That file could not be read.',

    'ms.step1_html': 'Open the <a href="https://portal.azure.com/" target="_blank" rel="noopener">Azure Portal</a> &rarr; <em>Microsoft Entra ID &rarr; App registrations &rarr; New registration</em>.',
    'ms.step2_html': 'Account types: personal <b>and</b> work/school.',
    'ms.step3_html': 'Redirect URI &rarr; platform <b>Mobile and desktop applications</b> &rarr; paste the address below.',
    'ms.step4_html': '<em>API permissions &rarr; Microsoft Graph &rarr; Delegated</em>: <code>Files.ReadWrite</code>, <code>offline_access</code>, <code>User.Read</code>.',
    'ms.step5_html': 'Copy the <b>Application (client) ID</b> below. A desktop registration needs no secret.',

    'field.redirect': 'Redirect URI',
    'field.redirect.toRegister': 'Redirect URI to register',
    'field.redirect.hint': 'A Desktop app client accepts this automatically; register it only if you chose Web application.',
    'field.copy': 'Copy',
    'field.copied': 'Copied',
    'field.clientId': 'Client ID',
    'field.clientSecret': 'Client secret',
    'field.appClientId': 'Application (client) ID',
    'field.secretOptional': '— leave empty for a desktop registration',
    'field.accountTypes': 'Account types',
    'tenant.common': 'Personal and work/school',
    'tenant.consumers': 'Personal only',
    'tenant.organizations': 'Work/school only',
    'field.save': 'Save',
    'field.clear': 'Clear',
    'field.saving': 'Saving…',
    'field.saved': 'Saved.',
    'field.savedPublic': 'Saved as a desktop registration (no secret needed).',
    'field.needClientId': 'A client ID is required.',
    'field.confirmClear': 'Clear the saved {provider} application settings?',

    // -- step 2
    'accounts.heading': 'Connect both accounts',
    'account.note': 'Read and write, so files can travel either way.',
    'account.readonly_html': 'This account is connected with read-only access, so files cannot be written into Drive. Disconnect and connect again to transfer <strong>from OneDrive into Google Drive</strong>.',
    'account.connect': 'Connect',
    'account.disconnect': 'Disconnect',
    'account.connected': 'connected',
    'account.notConnected': 'not connected',
    'account.finishSetup': 'finish step 1 first',
    'account.confirmDisconnect': 'Disconnect {provider}? Stored tokens for it are deleted.',
    'account.disconnected': '{provider} disconnected.',

    // -- step 3
    'pick.heading': 'Choose what moves where',
    'direction.label': 'Direction',
    'direction.gdrive_to_onedrive': 'Google Drive to OneDrive',
    'direction.onedrive_to_gdrive': 'OneDrive to Google Drive',
    'swap.label': 'Swap',
    'swap.title': 'Swap the two sides',
    'picker.up': 'Up',
    'picker.newFolder': '+ Folder',
    'picker.from': 'From {provider}',
    'picker.to': 'To {provider}',
    'picker.nothingSelected': 'Nothing selected',
    'picker.selected': 'Selected: {path} — the whole folder',
    'picker.selectedItems': 'Selected: {path} — {n} ticked',
    'picker.selectHint': 'Tick items to move only those. With nothing ticked, the whole folder moves.',
    'picker.connectFirst': 'Connect {provider} to browse your folders.',
    'picker.empty': 'This folder is empty.',
    'picker.loading': 'Loading…',
    'picker.needsConversion': 'converts to Office',
    'picker.shortcut': 'shortcut',

    'options.conflict': 'If a file of the same name is already there',
    'conflict.rename': 'Keep both (add a number)',
    'conflict.replace': 'Replace it',
    'conflict.skip': 'Leave it and skip',
    'options.verify': 'Verify every file by hash after transfer',
    'options.trashed': "Include files in the source's trash",

    'start.button': 'Start transfer',
    'start.hint': 'To start: {reasons}.',
    'start.ready': '{source} → {dest}, the whole folder. Files are counted first, then moved.',
    'start.readySelected': '{source} → {dest}, {n} ticked item(s). Files are counted first, then moved.',
    'start.started': 'Transfer #{id} started. Counting files…',
    'start.failed': 'Could not start: {error}',
    'reason.setup': 'finish the setup step',
    'reason.connectGoogle': 'connect Google Drive',
    'reason.connectOnedrive': 'connect OneDrive',
    'reason.pickSource': 'pick a source folder',
    'reason.pickDest': 'pick a destination folder',
    'reason.reconnectGoogle': 'reconnect Google Drive for write access',
    'warn.readonlyDirection': 'Google Drive is connected with read-only access. Disconnect it in step 2 and connect again to transfer files into Drive.',

    // -- step 4
    'jobs.heading': 'Transfers',
    'status.pending': 'queued',
    'status.planning': 'counting files',
    'status.running': 'running',
    'status.paused': 'paused',
    'status.completed': 'completed',
    'status.completed_with_errors': 'completed with errors',
    'status.failed': 'failed',
    'status.cancelled': 'cancelled',
    'jobs.empty': 'No transfers yet.',
    'job.files': '{done}/{total} files',
    'job.bytes': '{done} of {total}',
    'job.rate': '{rate}/s',
    'job.eta': '~{time} left',
    'job.failed': '{n} failed',
    'job.skipped': '{n} skipped',
    'job.pause': 'Pause',
    'job.cancel': 'Cancel',
    'job.resume': 'Resume',
    'job.retry': 'Retry failed',
    'job.showFiles': 'Show files',
    'job.hideFiles': 'Hide files',
    'job.report': 'Download report',
    'job.delete': 'Delete',
    'job.confirmDelete': 'Delete transfer #{id}? Files already transferred stay where they are.',
    'job.confirmDeleteRunning': 'Transfer #{id} is still running. Stop it and delete it? Files already transferred stay where they are.',
    'job.stopping': 'Stopping…',
    'job.requeued': 'Re-queued {n} failed file(s).',
    'table.status': 'Status',
    'table.destination': 'Destination',
    'table.size': 'Size',
    'table.noFiles': 'No files.',
    'table.showing': 'Showing {shown} of {total}. Download the report for the full list.',
    'table.renamedFrom': 'renamed from "{name}"',
    'table.converted': 'converted to an Office format',

    // -- notes
    'notes.summary': 'How this works, and what it cannot do',
    'notes.p1_html': '<strong>No local copy.</strong> Each file is read in byte ranges, held one chunk at a time in memory, and pushed straight into the other side\'s upload session. Nothing reaches a filesystem, so there is no temporary folder and no partial file left behind.',
    'notes.p2_html': '<strong>Bytes still travel through this program.</strong> A true cloud-to-cloud copy is not possible: Google Drive offers no &ldquo;send to URL&rdquo; API, and Microsoft retired its &ldquo;upload from URL&rdquo; feature. Something has to relay. Run this on a server or container and your own machine carries no traffic at all.',
    'notes.p3_html': '<strong>No password is ever asked for.</strong> Google and Microsoft stopped letting programs sign in with an email address and password. Authorisation happens on their own pages; this program never sees your password and can be cut off at any time from your account settings.',
    'notes.p4_html': '<strong>Google Docs, Sheets and Slides are converted.</strong> Native Google formats have no bytes to copy, so they are exported to .docx/.xlsx/.pptx on the way out, with a 10&nbsp;MB limit Google sets. Coming the other way nothing needs converting &mdash; except a OneNote notebook, which is a package with no single stream to copy.',
    'notes.p5_html': '<strong>Interrupted transfers resume.</strong> The destination remembers how much of each upload it holds. After a pause, a crash or a network drop, each file continues from that exact byte instead of starting over.',
    'notes.p6_html': '<strong>Renames are recorded, never silent.</strong> Characters the destination rejects, duplicate names and reserved names are rewritten, and every change appears in the job report.',

    // -- modal
    'modal.newFolder': 'New folder',
    'modal.folderName': 'Folder name',
    'modal.cancel': 'Cancel',
    'modal.create': 'Create',
    'modal.createdAs': 'Created as "{name}" — {reasons}.',
    'modal.createFailed': 'Could not create the folder: {error}',

    'error.status': 'Could not read status: {error}',
    'error.setup': 'Could not read the setup state: {error}',
    'connected.banner': '{provider} connected.',
  },

  tr: {
    'lang.switch': 'English',
    'lang.switch.title': 'Arayüzü İngilizceye çevir',

    'app.tagline': 'Google Drive ve OneDrive arasında, iki yönde de, {size} MiB’lik parçalar hâlinde. Diske hiçbir şey yazılmaz.',
    'app.meta': 'v{version} · aynı anda {parallel} dosya',

    // -- 1. adım
    'setup.heading': 'Erişim ayarı',
    'setup.checking': 'bakılıyor…',
    'setup.show': 'Göster',
    'setup.hide': 'Gizle',
    'setup.pill.done': 'tamam',
    'setup.pill.needed': 'gerekli',
    'setup.lede_html': 'Google ve Microsoft, hiçbir programın e-posta adresiniz ve şifrenizle giriş yapmasına izin vermiyor &mdash; bu güvenlik için kapatıldı ve dosya servisleri yalnızca yetki jetonu kabul ediyor. Bu yüzden her ikisinde de kendi hesabınızda bir uygulama tanımlamanız gerekiyor. Ücretsizdir, birkaç dakika sürer ve <strong>yalnızca bir kez yapılır</strong>.',
    'setup.state.notSetUp': 'ayarlanmadı',
    'setup.state.ready': 'hazır',
    'setup.state.env': 'ortam değişkeninden',

    'google.step1_html': '<a href="https://console.cloud.google.com/" target="_blank" rel="noopener">Google Cloud Console</a> adresini açın ve bir proje oluşturun.',
    'google.step2_html': '<em>APIs &amp; Services &rarr; Library</em> &rarr; <b>Google Drive API</b>’yi etkinleştirin.',
    'google.step3_html': '<em>OAuth consent screen</em> &rarr; External &rarr; kendi e-posta adresinizi test kullanıcısı olarak ekleyin.',
    'google.step4_html': '<em>Credentials &rarr; Create credentials &rarr; OAuth client ID</em> &rarr; <b>Desktop app</b> seçin.',
    'google.step5_html': 'Google size bir <b>Download JSON</b> düğmesi sunar. O dosyayı kaydedin ve aşağıdan seçin &mdash; elle hiçbir şey kopyalamanız gerekmez.',

    'import.label': 'Google’ın verdiği JSON dosyası',
    'import.button': 'Dosya seç…',
    'import.hint': 'Genellikle İndirilenler klasörünüzde, client_secret_….json adıyla durur. Aşağıdaki iki kutuyu sizin için doldurur.',
    'import.ok': '{name} dosyasından okundu. Şimdi Kaydet’e basın.',
    'import.bad': 'Bu dosya bir Google OAuth istemcisine benzemiyor. Credentials bölümünden yeniden indirin.',
    'import.unreadable': 'Dosya okunamadı.',

    'ms.step1_html': '<a href="https://portal.azure.com/" target="_blank" rel="noopener">Azure Portal</a> &rarr; <em>Microsoft Entra ID &rarr; App registrations &rarr; New registration</em>.',
    'ms.step2_html': 'Hesap türü: kişisel <b>ve</b> iş/okul hesapları.',
    'ms.step3_html': 'Redirect URI &rarr; platform olarak <b>Mobile and desktop applications</b> &rarr; aşağıdaki adresi yapıştırın.',
    'ms.step4_html': '<em>API permissions &rarr; Microsoft Graph &rarr; Delegated</em>: <code>Files.ReadWrite</code>, <code>offline_access</code>, <code>User.Read</code>.',
    'ms.step5_html': 'Aşağıya <b>Application (client) ID</b> değerini kopyalayın. Masaüstü kaydı için gizli anahtar gerekmez.',

    'field.redirect': 'Yönlendirme adresi',
    'field.redirect.toRegister': 'Kaydedilecek yönlendirme adresi',
    'field.redirect.hint': 'Desktop app istemcisi bunu kendiliğinden kabul eder; yalnızca Web application seçtiyseniz kaydetmeniz gerekir.',
    'field.copy': 'Kopyala',
    'field.copied': 'Kopyalandı',
    'field.clientId': 'Client ID',
    'field.clientSecret': 'Client secret',
    'field.appClientId': 'Application (client) ID',
    'field.secretOptional': '— masaüstü kaydında boş bırakın',
    'field.accountTypes': 'Hesap türleri',
    'tenant.common': 'Kişisel ve iş/okul',
    'tenant.consumers': 'Yalnızca kişisel',
    'tenant.organizations': 'Yalnızca iş/okul',
    'field.save': 'Kaydet',
    'field.clear': 'Temizle',
    'field.saving': 'Kaydediliyor…',
    'field.saved': 'Kaydedildi.',
    'field.savedPublic': 'Masaüstü kaydı olarak kaydedildi (gizli anahtar gerekmiyor).',
    'field.needClientId': 'Client ID gerekli.',
    'field.confirmClear': 'Kayıtlı {provider} uygulama ayarları silinsin mi?',

    // -- 2. adım
    'accounts.heading': 'İki hesabı da bağlayın',
    'account.note': 'Okuma ve yazma; dosyalar iki yönde de gidebilsin diye.',
    'account.readonly_html': 'Bu hesap salt okunur izinle bağlı, bu yüzden Drive’a dosya yazılamıyor. <strong>OneDrive’dan Google Drive’a</strong> aktarım için bağlantıyı kesip yeniden bağlanın.',
    'account.connect': 'Bağlan',
    'account.disconnect': 'Bağlantıyı kes',
    'account.connected': 'bağlı',
    'account.notConnected': 'bağlı değil',
    'account.finishSetup': 'önce 1. adımı tamamlayın',
    'account.confirmDisconnect': '{provider} bağlantısı kesilsin mi? Saklanan jetonlar silinir.',
    'account.disconnected': '{provider} bağlantısı kesildi.',

    // -- 3. adım
    'pick.heading': 'Ne nereye gidecek',
    'direction.label': 'Yön',
    'direction.gdrive_to_onedrive': 'Google Drive’dan OneDrive’a',
    'direction.onedrive_to_gdrive': 'OneDrive’dan Google Drive’a',
    'swap.label': 'Ters çevir',
    'swap.title': 'İki tarafın yerini değiştir',
    'picker.up': 'Yukarı',
    'picker.newFolder': '+ Klasör',
    'picker.from': 'Kaynak: {provider}',
    'picker.to': 'Hedef: {provider}',
    'picker.nothingSelected': 'Seçim yok',
    'picker.selected': 'Seçildi: {path} — klasörün tamamı',
    'picker.selectedItems': 'Seçildi: {path} — {n} öğe işaretli',
    'picker.selectHint': 'Yalnızca belirli öğeleri aktarmak için işaretleyin. Hiçbiri işaretli değilse klasörün tamamı aktarılır.',
    'picker.connectFirst': 'Klasörleri görmek için {provider} hesabını bağlayın.',
    'picker.empty': 'Bu klasör boş.',
    'picker.loading': 'Yükleniyor…',
    'picker.needsConversion': 'Office’e dönüşür',
    'picker.shortcut': 'kısayol',

    'options.conflict': 'Hedefte aynı isimde dosya varsa',
    'conflict.rename': 'İkisini de tut (numara ekle)',
    'conflict.replace': 'Üzerine yaz',
    'conflict.skip': 'Dokunma, atla',
    'options.verify': 'Her dosyayı aktarım sonrası karma ile doğrula',
    'options.trashed': 'Kaynağın çöp kutusundaki dosyaları da dahil et',

    'start.button': 'Aktarımı başlat',
    'start.hint': 'Başlamak için: {reasons}.',
    'start.ready': '{source} → {dest}, klasörün tamamı. Önce dosyalar sayılır, sonra aktarılır.',
    'start.readySelected': '{source} → {dest}, işaretli {n} öğe. Önce dosyalar sayılır, sonra aktarılır.',
    'start.started': '#{id} numaralı aktarım başladı. Dosyalar sayılıyor…',
    'start.failed': 'Başlatılamadı: {error}',
    'reason.setup': 'kurulum adımını tamamlayın',
    'reason.connectGoogle': 'Google Drive’ı bağlayın',
    'reason.connectOnedrive': 'OneDrive’ı bağlayın',
    'reason.pickSource': 'kaynak klasörü seçin',
    'reason.pickDest': 'hedef klasörü seçin',
    'reason.reconnectGoogle': 'yazma izni için Google Drive’ı yeniden bağlayın',
    'warn.readonlyDirection': 'Google Drive salt okunur izinle bağlı. Drive’a dosya aktarmak için 2. adımda bağlantıyı kesip yeniden bağlanın.',

    // -- 4. adım
    'jobs.heading': 'Aktarımlar',
    'status.pending': 'sırada',
    'status.planning': 'dosyalar sayılıyor',
    'status.running': 'sürüyor',
    'status.paused': 'duraklatıldı',
    'status.completed': 'tamamlandı',
    'status.completed_with_errors': 'hatalarla tamamlandı',
    'status.failed': 'başarısız',
    'status.cancelled': 'iptal edildi',
    'jobs.empty': 'Henüz aktarım yok.',
    'job.files': '{done}/{total} dosya',
    'job.bytes': '{total} içinden {done}',
    'job.rate': '{rate}/sn',
    'job.eta': '~{time} kaldı',
    'job.failed': '{n} başarısız',
    'job.skipped': '{n} atlandı',
    'job.pause': 'Duraklat',
    'job.cancel': 'İptal et',
    'job.resume': 'Devam et',
    'job.retry': 'Başarısızları yeniden dene',
    'job.showFiles': 'Dosyaları göster',
    'job.hideFiles': 'Dosyaları gizle',
    'job.report': 'Raporu indir',
    'job.delete': 'Sil',
    'job.confirmDelete': '#{id} numaralı aktarım silinsin mi? Aktarılmış dosyalar yerinde kalır.',
    'job.confirmDeleteRunning': '#{id} numaralı aktarım hâlâ sürüyor. Durdurulup silinsin mi? Aktarılmış dosyalar yerinde kalır.',
    'job.stopping': 'Durduruluyor…',
    'job.requeued': '{n} başarısız dosya yeniden kuyruğa alındı.',
    'table.status': 'Durum',
    'table.destination': 'Hedef',
    'table.size': 'Boyut',
    'table.noFiles': 'Dosya yok.',
    'table.showing': '{total} dosyadan {shown} tanesi gösteriliyor. Tam liste için raporu indirin.',
    'table.renamedFrom': '"{name}" adından değiştirildi',
    'table.converted': 'Office biçimine dönüştürüldü',

    // -- açıklamalar
    'notes.summary': 'Bu nasıl çalışır, neyi yapamaz',
    'notes.p1_html': '<strong>Yerel kopya yok.</strong> Her dosya bayt aralıkları hâlinde okunur, bellekte tek seferde tek parça tutulur ve doğrudan karşı tarafın yükleme oturumuna yazılır. Hiçbir şey diske inmez; geçici klasör de yarım dosya da kalmaz.',
    'notes.p2_html': '<strong>Baytlar yine de bu programdan geçer.</strong> Gerçek bir buluttan buluta kopyalama mümkün değil: Google Drive’ın &ldquo;şu adrese gönder&rdquo; diye bir servisi yok, Microsoft da &ldquo;URL’den yükle&rdquo; özelliğini geri çekti. Bir aracı şart. Programı bir sunucuda ya da konteynerde çalıştırırsanız kendi bilgisayarınız hiç trafik taşımaz.',
    'notes.p3_html': '<strong>Hiçbir zaman şifre istenmez.</strong> Google ve Microsoft, programların e-posta ve şifreyle giriş yapmasına son verdi. Yetkilendirme kendi sayfalarında olur; bu program şifrenizi hiç görmez ve hesap ayarlarınızdan istediğiniz an erişimi kesebilirsiniz.',
    'notes.p4_html': '<strong>Google Docs, Sheets ve Slides dönüştürülür.</strong> Google’ın kendi biçimlerinin kopyalanacak baytı yoktur; çıkarken .docx/.xlsx/.pptx olurlar ve Google’ın koyduğu 10&nbsp;MB sınırı geçerlidir. Ters yönde dönüştürme gerekmez &mdash; tek istisna, tek bir akışı olmayan OneNote defterleridir.',
    'notes.p5_html': '<strong>Yarıda kalan aktarım devam eder.</strong> Hedef, her yüklemenin ne kadarını aldığını hatırlar. Duraklatma, çökme ya da bağlantı kopması sonrası her dosya baştan değil, tam o baytdan devam eder.',
    'notes.p6_html': '<strong>Yeniden adlandırmalar kayda geçer.</strong> Hedefin kabul etmediği karakterler, aynı isimler ve ayrılmış adlar değiştirilir; her değişiklik aktarım raporunda görünür.',

    // -- pencere
    'modal.newFolder': 'Yeni klasör',
    'modal.folderName': 'Klasör adı',
    'modal.cancel': 'Vazgeç',
    'modal.create': 'Oluştur',
    'modal.createdAs': '"{name}" adıyla oluşturuldu — {reasons}.',
    'modal.createFailed': 'Klasör oluşturulamadı: {error}',

    'error.status': 'Durum okunamadı: {error}',
    'error.setup': 'Kurulum durumu okunamadı: {error}',
    'connected.banner': '{provider} bağlandı.',
  },
};

const LANG_KEY = 'cloudtransfer.lang';

function detectLanguage() {
  try {
    const stored = localStorage.getItem(LANG_KEY);
    if (stored && TRANSLATIONS[stored]) return stored;
  } catch {
    // Private browsing or blocked storage: fall through to the browser's own.
  }
  const preferred = (navigator.languages || [navigator.language || 'en'])
    .map((code) => String(code).toLowerCase().split('-')[0]);
  return preferred.find((code) => TRANSLATIONS[code]) || 'en';
}

let currentLang = detectLanguage();

function currentLanguage() {
  return currentLang;
}

/** Look up a key, filling {placeholders}. Missing keys fall back to English. */
function t(key, values) {
  const table = TRANSLATIONS[currentLang] || TRANSLATIONS.en;
  let text = table[key];
  if (text === undefined) text = TRANSLATIONS.en[key];
  if (text === undefined) return key;
  if (!values) return text;
  return text.replace(/\{(\w+)\}/g, (match, name) =>
    Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : match
  );
}

/** Replace every marked node in the document with the current language. */
function applyTranslations(root = document) {
  root.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-html]').forEach((node) => {
    node.innerHTML = t(node.dataset.i18nHtml);
  });
  root.querySelectorAll('[data-i18n-attr]').forEach((node) => {
    // "title:swap.title,placeholder:modal.folderName"
    node.dataset.i18nAttr.split(',').forEach((pair) => {
      const [attr, key] = pair.split(':').map((part) => part.trim());
      if (attr && key) node.setAttribute(attr, t(key));
    });
  });
  document.documentElement.lang = currentLang;
}

function setLanguage(lang) {
  if (!TRANSLATIONS[lang]) return;
  currentLang = lang;
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {
    // Not being able to remember the choice is not worth failing over.
  }
  applyTranslations();
  document.dispatchEvent(new CustomEvent('languagechange-app'));
}

function otherLanguage() {
  return currentLang === 'tr' ? 'en' : 'tr';
}
