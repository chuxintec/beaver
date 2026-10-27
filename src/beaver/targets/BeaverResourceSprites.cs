// Optional companion installed only for resource.sprites operations.
// Keeping the Sprite Data Provider assembly reference out of the core helper lets
// font/atlas/import/Spine steps compile in projects without com.unity.2d.sprite.
#if UNITY_EDITOR
using System;
using System.Linq;
using System.Collections.Generic;
using UnityEngine;
using UnityEditor;
using UnityEditor.U2D.Sprites;

namespace Beaver.Tools {
public static class BeaverResourceSprites {
    sealed class OriginalSpriteIdentity {
        public string name,assetGuid,spriteId;
        public long localId;
    }

    static OriginalSpriteIdentity[] CaptureOriginalSprites(string assetPath,TextureImporter importer,out TextureImporterType originalTextureType) {
        originalTextureType=importer.textureType;
        var sprites=AssetDatabase.LoadAllAssetsAtPath(assetPath).OfType<Sprite>().ToArray();
        if(sprites.Length==0)return new OriginalSpriteIdentity[0];
        var factory=new SpriteDataProviderFactories();factory.Init();var data=factory.GetSpriteEditorDataProviderFromObject(importer);
        if(data==null)throw new Exception("Cannot snapshot pre-existing Sprite identity before importer mutation");
        data.InitSpriteEditorDataProvider();var rects=data.GetSpriteRects();var idProvider=data.GetDataProvider<ISpriteNameFileIdDataProvider>();
        if(idProvider==null)throw new Exception("Cannot snapshot pre-existing Sprite GUID mapping before importer mutation");
        var pairs=idProvider.GetNameFileIdPairs().ToArray();
        return sprites.Select(sprite=>{
            string assetGuid;long localId;AssetDatabase.TryGetGUIDAndLocalFileIdentifier(sprite,out assetGuid,out localId);
            var spriteRows=rects.Where(x=>x.name==sprite.name).ToArray();var idRows=pairs.Where(x=>x.name==sprite.name).ToArray();
            if(spriteRows.Length!=1||idRows.Length!=1)throw new Exception("Pre-existing Sprite identity is missing or ambiguous: "+sprite.name);
            var spriteId=spriteRows[0].spriteID.ToString();var pairGuid=idRows[0].GetFileGUID().ToString();
            if(!String.Equals(spriteId,pairGuid,StringComparison.OrdinalIgnoreCase))throw new Exception("Pre-existing Sprite rect/name GUID mapping disagrees: "+sprite.name);
            return new OriginalSpriteIdentity{name=sprite.name,assetGuid=assetGuid,localId=localId,spriteId=spriteId};
        }).ToArray();
    }

    public static void Apply(BeaverResourceProduction.Step s) {
        var assetPath=s.assetPath;
        var importer=AssetImporter.GetAtPath(assetPath) as TextureImporter;
        if(importer==null)throw new Exception("Sprite source must be an existing TextureImporter asset");
        if(s.slices==null||s.slices.Length==0||s.maxSize<32)throw new Exception("Explicit slices/maxSize required");
        if(s.pixelsPerUnit<=0)throw new Exception("Explicit positive pixelsPerUnit required");
        TextureImporterType originalTextureType;var originalSprites=CaptureOriginalSprites(assetPath,importer,out originalTextureType);
        if(originalTextureType!=TextureImporterType.Sprite&&originalSprites.Length>0)throw new Exception("Original TextureImporterType reports non-Sprite but Sprite subassets are present; refusing ambiguous identity snapshot");
        foreach(var old in originalSprites){
            var declared=s.slices.FirstOrDefault(x=>x.name==old.name);
            if(declared==null||declared.fileId!=old.localId)throw new Exception("Existing Sprite name/localID omitted or changed; removal/re-ID is refused: "+old.name);
            if(!String.Equals(old.spriteId,declared.spriteId,StringComparison.OrdinalIgnoreCase))throw new Exception("Existing Sprite spriteID changed: "+old.name);
        }
        importer.textureType=TextureImporterType.Sprite;importer.spriteImportMode=SpriteImportMode.Multiple;importer.spritePixelsPerUnit=s.pixelsPerUnit;importer.maxTextureSize=s.maxSize;importer.alphaIsTransparency=s.alphaIsTransparency;importer.SaveAndReimport();
        importer=AssetImporter.GetAtPath(assetPath) as TextureImporter;
        var factory=new SpriteDataProviderFactories();factory.Init();var data=factory.GetSpriteEditorDataProviderFromObject(importer);
        if(data==null)throw new Exception("ISpriteEditorDataProvider unavailable");data.InitSpriteEditorDataProvider();
        var capProvider=data.GetDataProvider<ISpriteFrameEditCapability>();
        if(capProvider==null||!capProvider.GetEditCapability().HasCapability(EEditCapability.CreateAndDeleteSprite))throw new Exception("Importer does not support Sprite create/edit capability");
        var idProvider=data.GetDataProvider<ISpriteNameFileIdDataProvider>();
        if(idProvider==null)throw new Exception("Sprite name-fileID provider unavailable");
        var prior=data.GetSpriteRects();var priorIds=idProvider.GetNameFileIdPairs().ToArray();
        var importedAfter=AssetDatabase.LoadAllAssetsAtPath(assetPath).OfType<Sprite>().ToArray();
        foreach(var old in originalSprites){
            var oldRect=prior.FirstOrDefault(x=>x.name==old.name);var oldPair=priorIds.FirstOrDefault(x=>x.name==old.name);
            if(oldRect==null||!String.Equals(oldRect.spriteID.ToString(),old.spriteId,StringComparison.OrdinalIgnoreCase))throw new Exception("Importer reimport changed a pre-existing Sprite GUID: "+old.name);
            if(oldPair==null||!String.Equals(oldPair.GetFileGUID().ToString(),old.spriteId,StringComparison.OrdinalIgnoreCase))throw new Exception("Importer reimport changed a pre-existing Sprite file GUID mapping: "+old.name);
            var imported=importedAfter.Where(x=>x.name==old.name).ToArray();
            if(imported.Length!=1)throw new Exception("Importer reimport removed or duplicated a pre-existing Sprite: "+old.name);
            string guid;long localId;AssetDatabase.TryGetGUIDAndLocalFileIdentifier(imported[0],out guid,out localId);
            if(guid!=old.assetGuid||localId!=old.localId)throw new Exception("Importer reimport changed a pre-existing Sprite asset GUID/localID: "+old.name);
        }
        foreach(var x in s.slices)if(x.rect==null||x.rect.Length!=4||x.border==null||x.border.Length!=4||x.pivot==null||x.pivot.Length!=2||String.IsNullOrEmpty(x.name)||String.IsNullOrEmpty(x.spriteId))throw new Exception("Malformed Sprite slice");
        var texture=AssetDatabase.LoadAssetAtPath<Texture2D>(assetPath);if(texture==null)throw new Exception("Texture asset load failed");
        foreach(var x in s.slices){var rect=new Rect(x.rect[0],x.rect[1],x.rect[2],x.rect[3]);if(rect.xMin<0||rect.yMin<0||rect.xMax>texture.width||rect.yMax>texture.height)throw new Exception("Sprite rect outside source dimensions: "+x.name);}
        var editCapability=capProvider.GetEditCapability();
        foreach(var required in new[]{EEditCapability.EditSpriteName,EEditCapability.EditSpriteRect,EEditCapability.EditBorder,EEditCapability.EditPivot})if(!editCapability.HasCapability(required))throw new Exception("Importer lacks required Sprite edit capability: "+required);
        var rows=s.slices.Select(x=>{var rect=new SpriteRect();rect.name=x.name;rect.spriteID=new GUID(x.spriteId);rect.rect=new Rect(x.rect[0],x.rect[1],x.rect[2],x.rect[3]);rect.border=new Vector4(x.border[0],x.border[1],x.border[2],x.border[3]);rect.pivot=new Vector2(x.pivot[0],x.pivot[1]);rect.alignment=SpriteAlignment.Custom;return rect;}).ToArray();
        var pairs=s.slices.Select(x=>new SpriteNameFileIdPair(x.name,new GUID(x.spriteId))).ToList();
        data.SetSpriteRects(rows);idProvider.SetNameFileIdPairs(pairs);data.Apply();importer.SaveAndReimport();
    }

    public static void Verify(BeaverResourceProduction.Step s,BeaverResourceProduction.Result r) {
        var sprites=AssetDatabase.LoadAllAssetsAtPath(s.assetPath).OfType<Sprite>().ToArray();
        var importer=AssetImporter.GetAtPath(s.assetPath);var factory=new SpriteDataProviderFactories();factory.Init();var data=factory.GetSpriteEditorDataProviderFromObject(importer);
        if(data==null)throw new Exception("ISpriteEditorDataProvider unavailable for readback");data.InitSpriteEditorDataProvider();var rects=data.GetSpriteRects();
        r.sprites=sprites.Select(x=>{string guid;long localId;AssetDatabase.TryGetGUIDAndLocalFileIdentifier(x,out guid,out localId);var rect=rects.FirstOrDefault(z=>z.name==x.name);var pixelPivot=x.pivot;if(x.rect.width<=0||x.rect.height<=0)throw new Exception("Imported Sprite has zero-area rect; normalized pivot cannot be read: "+x.name);var normalizedPivot=new Vector2(pixelPivot.x/x.rect.width,pixelPivot.y/x.rect.height);return new BeaverResourceProduction.SpriteRow{name=x.name,guid=guid,spriteId=rect==null?"":rect.spriteID.ToString(),localId=localId,rect=x.rect,border=x.border,pivot=normalizedPivot,pixelPivot=pixelPivot};}).ToArray();
        var mismatches=(s.slices??new BeaverResourceProduction.Slice[0]).Select(expected=>new{expected,actual=r.sprites.Where(actual=>actual.name==expected.name).ToArray()}).Where(row=>!row.actual.Any(actual=>String.Equals(actual.spriteId,row.expected.spriteId,StringComparison.OrdinalIgnoreCase)&&(row.expected.fileId==0||actual.localId==row.expected.fileId)&&actual.rect==new Rect(row.expected.rect[0],row.expected.rect[1],row.expected.rect[2],row.expected.rect[3])&&actual.border==new Vector4(row.expected.border[0],row.expected.border[1],row.expected.border[2],row.expected.border[3])&&actual.pivot==new Vector2(row.expected.pivot[0],row.expected.pivot[1]))).ToArray();
        if(mismatches.Length>0){
            var details=mismatches.Select(row=>"expected{"+DescribeExpected(row.expected)+"} actual{"+(row.actual.Length==0?"none by name":String.Join(" | ",row.actual.Select(DescribeActual).ToArray()))+"}");
            throw new Exception("Sprite identity/geometry readback mismatch: "+String.Join("; ",details.ToArray()));
        }
    }
    static string F(float value) { return value.ToString("R",System.Globalization.CultureInfo.InvariantCulture); }
    static string DescribeExpected(BeaverResourceProduction.Slice x) { return "name="+x.name+",spriteId="+x.spriteId+",localId="+(x.fileId==0?"<Unity-generated>":x.fileId.ToString(System.Globalization.CultureInfo.InvariantCulture))+",rect="+String.Join(",",x.rect.Select(F).ToArray())+",border="+String.Join(",",x.border.Select(F).ToArray())+",pivot="+String.Join(",",x.pivot.Select(F).ToArray()); }
    static string DescribeActual(BeaverResourceProduction.SpriteRow x) { return "name="+x.name+",assetGuid="+x.guid+",spriteId="+x.spriteId+",localId="+x.localId.ToString(System.Globalization.CultureInfo.InvariantCulture)+",rect="+F(x.rect.x)+","+F(x.rect.y)+","+F(x.rect.width)+","+F(x.rect.height)+",border="+F(x.border.x)+","+F(x.border.y)+","+F(x.border.z)+","+F(x.border.w)+",normalizedPivot="+F(x.pivot.x)+","+F(x.pivot.y)+",pixelPivot="+F(x.pixelPivot.x)+","+F(x.pixelPivot.y); }
}
}
#endif
