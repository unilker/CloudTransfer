# Kurulum (Türkçe)

CloudTransfer, Google Drive ile OneDrive arasında **her iki yönde** ve
**diske hiç yazmadan** dosya aktarır. Dosya kaynaktan parça parça okunur,
bellekte tek bir parça tutulur ve doğrudan hedefe yazılır. Geçici klasör
yoktur, yarım dosya kalmaz.

Yön, arayüzdeki bir düğmeyle seçilir: **Google Drive → OneDrive** ya da
**OneDrive → Google Drive**.

> **Arayüz Türkçedir.** Tarayıcınızın dili Türkçeyse program kendiliğinden
> Türkçe açılır; sağ üstteki düğmeyle İngilizceye geçebilirsiniz.

> **Şifreyle giriş neden yok?** Google ve Microsoft, programların e-posta ve
> şifreyle giriş yapmasına izin vermiyor; bu güvenlik gerekçesiyle kapatıldı ve
> dosya servisleri yalnızca yetki jetonu kabul ediyor. Bu yüzden aşağıdaki
> uygulama kaydı adımı zorunlu — ama yalnızca bir kez yapılır.

> **Önemli:** İki bulutu birbiriyle doğrudan konuşturmak mümkün değil. Google
> Drive'ın "şu adrese gönder" diye bir API'si yok; Microsoft'un "URL'den yükle"
> özelliği ise geri çekildi. Bu yüzden baytların bir aracıdan geçmesi gerekiyor.
> Bu yazılım o aracı. Bilgisayarınıza hiç bayt gelmemesini istiyorsanız
> uygulamayı bir sunucuda veya konteynerde çalıştırın; Windows kurulumunda
> aktarım sizin bağlantınızı kullanır.

---

## 1. Programı kurun

### Windows

[Sürümler sayfasından](https://github.com/unilker/CloudTransfer/releases)
`CloudTransfer-Setup-<sürüm>.exe` dosyasını indirip çalıştırın. Yalnızca sizin
kullanıcınıza kurulur; yönetici yetkisi gerekmez.

Windows SmartScreen "yayıncı bilinmiyor" uyarısı verecek. Kurulum dosyası
imzalı değil (imza ücretli bir sertifika gerektiriyor). **Ek bilgi → Yine de
çalıştır** deyin, ya da kaynaktan kendiniz derleyin.

Program açıldığında arayüz tarayıcınızda açılır ve sistem tepsisinde bir simge
bırakır. Çıkmak için o simgeye sağ tıklayıp **Quit** deyin.

Ayarlarınız, bağlı hesaplarınız ve aktarım geçmişiniz
`%LOCALAPPDATA%\CloudTransfer` klasöründe tutulur; bir sorun olursa aynı yerdeki
günlük dosyasına bakın.

### Kaynaktan (her platform)

```bash
python -m venv .venv && .venv/bin/pip install -e ".[desktop]"
```

```bash
.venv/bin/cloudtransfer desktop
```

### Konteynerde (bilgisayarınıza hiç trafik gelmez)

```bash
docker compose up -d
```

---

## 2. İlk açılışta erişim ayarı

Program sizi adım adım yönlendirir; **hiçbir dosyayı elle düzenlemeniz
gerekmez**. Ancak Google ve Microsoft, dosyalarınıza erişecek her program için
sizin adınıza kayıtlı bir uygulama ister. Bunun etrafından dolaşmak mümkün
değil: doğrulanmamış üçüncü taraf bir uygulamaya hesabınız adına erişim
verilemiyor.

### Google tarafı

1. [Google Cloud Console](https://console.cloud.google.com/) → proje oluşturun.
2. **APIs & Services → Library** → **Google Drive API**'yi etkinleştirin.
3. **OAuth consent screen** → External → kendinizi test kullanıcısı ekleyin.
4. **Credentials → Create credentials → OAuth client ID → Desktop app.**
5. Google size bir **Download JSON** düğmesi gösterir. O dosyayı kaydedin.
6. Programdaki 1. adımda **Dosya seç…** deyip o dosyayı gösterin — Client ID ve
   Client secret kutuları kendiliğinden dolar. Sonra **Kaydet**.

   (İsterseniz iki değeri elle de yapıştırabilirsiniz; JSON dosyası sadece
   yazım hatası ihtimalini ortadan kaldırır.)

Kullanılan izin `https://www.googleapis.com/auth/drive`'dır. Tam erişim
gerekiyor, çünkü dosyalar iki yönde de gidiyor: salt okuma izni Drive'dan
OneDrive'a göndermeye yeter ama OneDrive'dan Drive'a **almaya** yetmez.

> **Daha önceki bir sürümü kullandıysanız:** O zaman bağladığınız Google hesabı
> salt okunur izinle bağlıdır ve bu izin kendiliğinden genişlemez. Drive'a
> **doğru** yapılan aktarımlar açık bir mesajla reddedilir. Düzeltmek için
> 2. adımda Google Drive'ı **Disconnect** edip yeniden bağlayın. Drive'dan
> **dışarı** aktarım her durumda çalışmaya devam eder.

### Microsoft tarafı

1. [Azure Portal](https://portal.azure.com/) → **Microsoft Entra ID →
   App registrations → New registration**.
2. Hesap türü: kişisel **ve** iş/okul hesapları.
3. Redirect URI → platform olarak **Mobile and desktop applications** seçin ve
   programdaki 1. adımda gösterilen adresi yapıştırın (yanındaki **Copy**
   düğmesi işinizi görür).
4. **API permissions → Microsoft Graph → Delegated**: `Files.ReadWrite`,
   `offline_access`, `User.Read`.
5. **Application (client) ID** değerini programa yapıştırın. Masaüstü kaydı için
   **client secret gerekmez**; o kutuyu boş bırakın.

> Microsoft tarafında secret oluşturursanız, **Value** sütunundaki değeri
> kopyalamanız gerekir, Secret ID'yi değil. İnsanların en çok takıldığı yer
> burası — ama masaüstü kaydında zaten gerek yok.

İki kutu da yeşil "ready" olduğunda 1. adım kendiliğinden kapanır ve bir daha
karşınıza çıkmaz.

---

## 3. Kullanım

1. 2. adımda iki hesabı da **Connect** ile bağlayın.
2. 3. adımda önce **yönü** seçin (ya da **Swap** ile ters çevirin).
3. Soldan kaynak klasörü, sağdan hedef klasörü seçin.
4. **Yalnızca belirli dosya veya klasörleri** aktarmak istiyorsanız soldaki
   listede onları işaretleyin. Bir klasörü işaretlemek o klasörün tamamını
   seçer. Hiçbir şey işaretlemezseniz klasörün tümü aktarılır.
5. Aynı isim varsa ne olacağını seçip **Aktarımı başlat** deyin.

Aktarım listesindeki her işi **Duraklat**, **İptal et** ve **Sil**
düğmeleriyle yönetebilirsiniz. Duraklatma dosyaların ortasında da çalışır;
devam ettiğinizde kaldığı baytdan sürer. Silme, iş hâlâ sürüyorsa önce
durdurur. Aktarılmış dosyalar her durumda hedefte kalır.

Aktarım sırasında duraklatabilirsiniz; kaldığı **baytdan** devam eder. Bittiğinde
**Download report** ile her dosyanın durumunu, yeniden adlandırmaları ve atlanan
dosyaların gerekçelerini CSV olarak indirebilirsiniz.

---

## Kurulum "dosyalar kullanımda" diyorsa

CloudTransfer bildirim alanından (saatin yanından) çalışır. Açıkken Windows
dosyalarını kilitli tutar; bu yüzden ne kurulum ne de kaldırma onları
değiştirebilir.

Kurulum bunu fark edip sizden programı kapatmanızı ister. Yine de karşılaşırsanız:

1. Görev çubuğunun **sağ alt** köşesindeki bildirim alanında CloudTransfer
   simgesine sağ tıklayıp **Quit** deyin. Simge görünmüyorsa oradaki küçük
   **ok** işaretine tıklayıp gizli simgeleri açın.
2. Simge yoksa **Görev Yöneticisi** → **Ayrıntılar** sekmesi →
   **CloudTransfer.exe** → **Görevi sonlandır**.
3. Kurulumu yeniden çalıştırın.

Kaldırma işlemi bu yüzden yarıda kaldıysa ve klasör geride kaldıysa, yeniden
kurmadan önce şu klasörü silin:

```
%LOCALAPPDATA%\Programs\CloudTransfer
```

Ayarlarınız, bağlı hesaplarınız ve aktarım geçmişiniz **başka** bir yerde
tutulur ve bundan etkilenmez:

```
%LOCALAPPDATA%\CloudTransfer
```

---

## Tarayıcı "Bu siteye ulaşılamıyor" diyorsa

Program açılıp tarayıcıyı açtı ama sayfa gelmiyorsa, sunucu ya hiç başlamadı ya
da başka bir portta çalışıyor demektir.

1. **Günlük dosyasına bakın.** Dosya Gezgini'nin adres çubuğuna şunu yapıştırın:

   ```
   %LOCALAPPDATA%\CloudTransfer
   ```

   Oradaki `cloudtransfer.log` dosyasının son satırları ya
   `CloudTransfer is serving at http://127.0.0.1:...` der (o adresi tarayıcıya
   yazın) ya da başlayamama sebebini yazar.

2. **Aynı klasördeki `runtime.json`** çalışan örneğin gerçek adresini tutar.

3. Hâlâ açılmıyorsa: tepsi simgesinden **Quit** deyin ya da Görev
   Yöneticisi'nden `CloudTransfer.exe` süreçlerini kapatıp programı yeniden
   başlatın.

---

## Bilmeniz gerekenler

- **Google Docs/Sheets/Slides** dosyaları Drive'dan çıkarken `.docx`/`.xlsx`/
  `.pptx` olarak dönüştürülür. Google'ın dışa aktarma sınırı 10 MB'dır; bunu
  aşanlar gerekçesi yazılarak atlanır.
- **Google Forms, Sites ve My Maps** dışa aktarılamaz; bunlar atlanır.
- **OneNote defterleri** kopyalanamaz; tek bir dosya değil, paket oldukları
  için atlanır.
- Ters yönde (OneDrive → Drive) hiçbir dönüştürme yapılmaz, çünkü OneDrive'daki
  her dosya zaten gerçek bayttır.
- **OneDrive'ın kabul etmediği karakterler** (`" * : < > ? / \ |`) OneDrive'a
  giderken değiştirilir, aynı klasördeki aynı isimli dosyalar numaralandırılır.
  Drive'a giderken böyle bir kısıt olmadığı için isimler olduğu gibi kalır. Her
  değişiklik rapora yazılır.
- **Hiçbir şeyi silmez.** Hangi yönde olursa olsun bu bir kopyalamadır;
  kaynaktan dosya kaldırılmaz.
- Her dosya aktarımdan sonra karma (hash) ile doğrulanır.
- Program 8765 numaralı portu kullanır. Meşgulse bir üstünü dener; o durumda
  1. adımdaki yönlendirme adresi de değişir ve ekranda güncel hâli görünür.

---

## Yeni bir sürüm yayınlamak

Terminal gerekmez, tarayıcıdan yapılır:

1. Depoda sağdaki **Releases** bölümüne girin → **Draft a new release**.
2. **Choose a tag** kutusuna sürümü yazın, örneğin `v1.3.0`, ve çıkan
   **Create new tag: v1.3.0 on publish** satırına tıklayın.
3. **Target** olarak `main` seçili olsun.
4. İsterseniz bir başlık yazın, sonra **Publish release** deyin.

Birkaç dakika içinde derleme çalışır ve kurulum dosyasını sağlamasıyla birlikte
o yayın sayfasına ekler. İlerlemeyi **Actions** sekmesinden izleyebilirsiniz.
