#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libhfs.h"
int main(int argc,char **argv){
 hfsvol *v; hfsfile *a,*b; unsigned char *buf; int i;
 if(argc!=2)return 2;
 v=hfs_mount(argv[1],0,HFS_MODE_RDWR|HFS_OPT_NOCACHE); if(!v){fprintf(stderr,"%s\n",hfs_error);return 1;}
 a=hfs_create(v,":TARGET","BINA","TEST"); b=hfs_create(v,":FILLER","BINA","TEST");
 if(!a||!b){fprintf(stderr,"create %s\n",hfs_error);return 1;}
 buf=malloc(65536);
 for(i=0;i<16;i++){
   memset(buf,0x41+i,65536); if(hfs_write(a,buf,65536)!=65536){fprintf(stderr,"wa %s\n",hfs_error);return 1;}
   memset(buf,0x80+i,65536); if(hfs_write(b,buf,65536)!=65536){fprintf(stderr,"wb %s\n",hfs_error);return 1;}
   if(hfs_flush(v)==-1){fprintf(stderr,"flush %s\n",hfs_error);return 1;}
 }
 free(buf); hfs_close(a);hfs_close(b);hfs_umount(v);return 0;
}
