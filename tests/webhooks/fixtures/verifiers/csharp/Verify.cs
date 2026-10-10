// Verify a Talent OS webhook (TalentOS-Signature: t=<ts>,v1=<hex>[,v1=<hex>]).
using System;
using System.Linq;
using System.Security.Cryptography;
using System.Text;

public static class TalentOsWebhook
{
    public static bool Verify(string secret, string header, byte[] body, long toleranceSeconds = 300)
    {
        var parts = header.Split(',').Select(p => p.Split('=', 2)).Where(p => p.Length == 2).ToList();
        var ts = parts.FirstOrDefault(p => p[0] == "t")?[1];
        if (ts == null || Math.Abs(DateTimeOffset.UtcNow.ToUnixTimeSeconds() - long.Parse(ts)) > toleranceSeconds) return false;
        using var mac = new HMACSHA256(Encoding.UTF8.GetBytes(secret));
        var signed = Encoding.UTF8.GetBytes(ts + ".").Concat(body).ToArray();
        var expected = Encoding.UTF8.GetBytes(Convert.ToHexString(mac.ComputeHash(signed)).ToLowerInvariant());
        return parts.Any(p => p[0] == "v1" && CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(p[1]), expected));
    }
}
