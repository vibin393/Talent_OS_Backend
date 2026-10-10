// Verify a Talent OS webhook (TalentOS-Signature: t=<ts>,v1=<hex>[,v1=<hex>]).
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class Verify {
    public static boolean verify(String secret, String header, byte[] body, long toleranceSeconds) throws Exception {
        String ts = null;
        for (String part : header.split(",")) if (part.startsWith("t=")) ts = part.substring(2);
        if (ts == null || Math.abs(System.currentTimeMillis() / 1000 - Long.parseLong(ts)) > toleranceSeconds) return false;
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        mac.update((ts + ".").getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder();
        for (byte b : mac.doFinal(body)) hex.append(String.format("%02x", b));
        byte[] expected = hex.toString().getBytes(StandardCharsets.UTF_8);
        for (String part : header.split(","))
            if (part.startsWith("v1=") && MessageDigest.isEqual(part.substring(3).getBytes(StandardCharsets.UTF_8), expected)) return true;
        return false;
    }
}
