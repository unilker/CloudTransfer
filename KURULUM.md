# Kurulum (Türkçe)

CloudTransfer, Google Drive'daki dosyaları **diske hiç yazmadan** OneDrive'a
aktarır. Dosya, Drive'dan parça parça okunur, bellekte tek bir parça tutulur ve
doğrudan OneDrive'a yazılır. Geçici klasör yoktur, yarım dosya kalmaz.

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
5. Çıkan **Client ID** ve **Client secret** değerlerini programdaki 1. adıma
   yapıştırıp **Save** deyin.

Kullanılan izin `drive.readonly`'dir — program Drive'ınızda hiçbir değişiklik
yapmaz.

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
2. 3. adımda soldan Drive klasörünü, sağdan OneDrive klasörünü seçin.
3. Aynı isim varsa ne olacağını seçip **Start transfer** deyin.

Aktarım sırasında duraklatabilirsiniz; kaldığı **baytdan** devam eder. Bittiğinde
**Download report** ile her dosyanın durumunu, yeniden adlandırmaları ve atlanan
dosyaların gerekçelerini CSV olarak indirebilirsiniz.

---

## Bilmeniz gerekenler

- **Google Docs/Sheets/Slides** dosyaları `.docx`/`.xlsx`/`.pptx` olarak
  dönüştürülür. Google'ın dışa aktarma sınırı 10 MB'dır; bunu aşanlar gerekçesi
  yazılarak atlanır.
- **Google Forms, Sites ve My Maps** dışa aktarılamaz; bunlar atlanır.
- **OneDrive'ın kabul etmediği karakterler** (`" * : < > ? / \ |`) değiştirilir,
  aynı klasördeki aynı isimli dosyalar numaralandırılır. Her değişiklik rapora
  yazılır.
- **Drive'dan hiçbir şey silinmez.** Erişim salt okunurdur; bu bir kopyalamadır.
- Her dosya aktarımdan sonra karma (hash) ile doğrulanır.
- Program 8765 numaralı portu kullanır. Meşgulse bir üstünü dener; o durumda
  1. adımdaki yönlendirme adresi de değişir ve ekranda güncel hâli görünür.
