package main

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"testing"

	oci "github.com/PingCAP-QE/ee-apps/dl/gen/oci"
)

type fakeOCIService struct {
	headPayload *oci.HeadFilePayload
	headResult  *oci.HeadFileResult
	headErr     error
}

func (f *fakeOCIService) ListFiles(context.Context, *oci.ListFilesPayload) ([]string, error) {
	panic("unexpected call")
}

func (f *fakeOCIService) DownloadFile(context.Context, *oci.DownloadFilePayload) (*oci.DownloadFileResult, io.ReadCloser, error) {
	panic("unexpected call")
}

func (f *fakeOCIService) HeadFile(_ context.Context, payload *oci.HeadFilePayload) (*oci.HeadFileResult, error) {
	f.headPayload = payload
	return f.headResult, f.headErr
}

func (f *fakeOCIService) DownloadFileSha256(context.Context, *oci.DownloadFileSha256Payload) (*oci.DownloadFileSha256Result, io.ReadCloser, error) {
	panic("unexpected call")
}

func TestHeadOCIMiddlewareUsesServiceHeadFile(t *testing.T) {
	svc := &fakeOCIService{
		headResult: &oci.HeadFileResult{
			Length:             123,
			ContentDisposition: `attachment; filename="artifact.tar.gz"; filename*=UTF-8''artifact.tar.gz`,
		},
	}
	handler := headOCIMiddleware(svc, log.New(io.Discard, "", 0))(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("next handler should not be called for matched HEAD request")
	}))

	req := httptest.NewRequest(http.MethodHead, "/oci-file/hub.pingcap.net/pingcap/tidb/package?tag=v1&file=artifact.tar.gz", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusOK)
	}
	if svc.headPayload == nil {
		t.Fatal("HeadFile payload was not passed to service")
	}
	if svc.headPayload.Repository != "hub.pingcap.net/pingcap/tidb/package" {
		t.Fatalf("repository = %q", svc.headPayload.Repository)
	}
	if svc.headPayload.Tag != "v1" {
		t.Fatalf("tag = %q", svc.headPayload.Tag)
	}
	if svc.headPayload.File == nil || *svc.headPayload.File != "artifact.tar.gz" {
		t.Fatalf("file = %#v", svc.headPayload.File)
	}
	if got := rec.Header().Get("Content-Length"); got != "123" {
		t.Fatalf("Content-Length = %q, want 123", got)
	}
	if got := rec.Header().Get("Content-Disposition"); got != svc.headResult.ContentDisposition {
		t.Fatalf("Content-Disposition = %q", got)
	}
}
